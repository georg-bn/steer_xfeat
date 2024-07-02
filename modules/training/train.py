"""
	"XFeat: Accelerated Features for Lightweight Image Matching, CVPR 2024."
	https://www.verlab.dcc.ufmg.br/descriptors/xfeat_cvpr24/
"""

import argparse
import os
import time
import sys

def parse_arguments():
    parser = argparse.ArgumentParser(description="XFeat training script.")

    parser.add_argument('--megadepth_root_path', type=str, default='/ssd/guipotje/Data/MegaDepth',
                        help='Path to the MegaDepth dataset root directory.')
    parser.add_argument('--synthetic_root_path', type=str, default='/homeLocal/guipotje/sshfs/datasets/coco_20k',
                        help='Path to the synthetic dataset root directory.')
    parser.add_argument('--ckpt_save_path', type=str, required=True,
                        help='Path to save the checkpoints.')
    parser.add_argument('--experiment_id', type=str, default='0',
                        help='Id of experiment, for logging.')
    parser.add_argument('--training_type', type=str, default='xfeat_default',
                        choices=['xfeat_default', 'xfeat_synthetic', 'xfeat_megadepth'],
                        help='Training scheme. xfeat_default uses both megadepth & synthetic warps.')
    parser.add_argument('--batch_size', type=int, default=10,
                        help='Batch size for training. Default is 10.')
    parser.add_argument('--n_steps', type=int, default=160_000,
                        help='Number of training steps. Default is 160000.')
    parser.add_argument('--lr', type=float, default=3e-4,
                        help='Learning rate. Default is 0.0003.')
    parser.add_argument('--gamma_steplr', type=float, default=0.5,
                        help='Gamma value for StepLR scheduler. Default is 0.5.')
    parser.add_argument('--steer90', action='store_true',
                        help='If set, train with 90 deg rotation augmentation and a permutation steerer.')
    parser.add_argument('--learnable_steer90', action='store_true',
                        help='If set, train with 90 deg rotation augmentation and a learned steerer.')
    parser.add_argument('--training_res', type=lambda s: tuple(map(int, s.split(','))),
                        default=(800, 608), help='Training resolution as width,height. Default is (800, 608).')
    parser.add_argument('--device_num', type=str, default='0',
                        help='Device number to use for training. Default is "0".')
    parser.add_argument('--num_workers', type=int, default=0,
                        help='Number of workers in DataLoader. Default is 0.')
    parser.add_argument('--dry_run', action='store_true',
                        help='If set, perform a dry run training with a mini-batch for sanity check.')
    parser.add_argument('--pretrained_weights', type=str, default=None,
                        help='Path to pretrained weights for initialization. Default is None.')
    parser.add_argument('--train_only_descriptor', action='store_true',
                        help='If set, train only descriptor part of network.')
    parser.add_argument('--save_ckpt_every', type=int, default=500,
                        help='Save checkpoints every N steps. Default is 500.')

    args = parser.parse_args()

    os.environ['CUDA_VISIBLE_DEVICES'] = args.device_num

    return args

args = parse_arguments()

import torch
from torch import nn
from torch import optim
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter

import numpy as np

from modules.model import *
from modules.dataset.augmentation import *
from modules.training.utils import *
from modules.training.losses import *

from modules.dataset.megadepth.megadepth import MegaDepthDataset
from modules.dataset.megadepth import megadepth_warper
from torch.utils.data import Dataset, DataLoader


class Trainer():
    """
        Class for training XFeat with default params as described in the paper.
        We use a blend of MegaDepth (labeled) pairs with synthetically warped images (self-supervised).
        The major bottleneck is to keep loading huge megadepth h5 files from disk, 
        the network training itself is quite fast.
    """

    def __init__(self, megadepth_root_path, 
                 synthetic_root_path, 
                 ckpt_save_path, 
                 experiment_id,
                 pretrained_weights=None,
                 train_only_descriptor=False,
                 model_name = 'xfeat_default',
                 batch_size = 10, n_steps = 160_000, lr= 3e-4, gamma_steplr=0.5, 
                 training_res = (800, 608), device_num="0", dry_run = False,
                 save_ckpt_every = 500, steer90=False, learnable_steer90=False,
                 num_workers=0):

        self.dev = torch.device ('cuda' if torch.cuda.is_available() else 'cpu')
        self.net = XFeatModel()
        if pretrained_weights is not None:
            self.net.load_state_dict(torch.load(pretrained_weights))
        self.net.to(self.dev)

        #Setup optimizer 
        self.batch_size = batch_size
        self.steps = n_steps
        self.train_only_descriptor = train_only_descriptor

        ##################### Synthetic COCO INIT ##########################
        if model_name in ('xfeat_default', 'xfeat_synthetic'):
            self.augmentor = AugmentationPipe(
                                        img_dir = synthetic_root_path,
                                        device = self.dev, load_dataset = True,
                                        batch_size = int(self.batch_size * 0.4 if model_name=='xfeat_default' else batch_size),
                                        out_resolution = training_res, 
                                        warp_resolution = training_res,
                                        sides_crop = 0.1,
                                        max_num_imgs = 3_000,
                                        num_test_imgs = 5,
                                        photometric = True,
                                        geometric = True,
                                        reload_step = 4_000
                                        )
        else:
            self.augmentor = None
        ##################### Synthetic COCO END #######################


        ##################### MEGADEPTH INIT ##########################
        if model_name in ('xfeat_default', 'xfeat_megadepth'):
            TRAIN_BASE_PATH = f"{megadepth_root_path}/train_data/megadepth_indices"
            TRAINVAL_DATA_SOURCE = f"{megadepth_root_path}/MegaDepth_v1"

            TRAIN_NPZ_ROOT = f"{TRAIN_BASE_PATH}/scene_info_0.1_0.7"

            npz_paths = glob.glob(TRAIN_NPZ_ROOT + '/*.npz')[:]
            data = torch.utils.data.ConcatDataset( [MegaDepthDataset(root_dir = TRAINVAL_DATA_SOURCE,
                            npz_path = path) for path in tqdm.tqdm(npz_paths, desc="[MegaDepth] Loading metadata")] )

            self.data_loader = DataLoader(data, 
                                          batch_size=int(self.batch_size * 0.6 if model_name=='xfeat_default' else batch_size),
                                          num_workers=num_workers,
                                          shuffle=True)
            self.data_iter = iter(self.data_loader)

        else:
            self.data_iter = None
        ##################### MEGADEPTH INIT END #######################

        os.makedirs(ckpt_save_path, exist_ok=True)
        os.makedirs(ckpt_save_path + '/logdir', exist_ok=True)

        self.dry_run = dry_run
        self.save_ckpt_every = save_ckpt_every
        self.ckpt_save_path = ckpt_save_path
        if learnable_steer90 and steer90:
            raise ValueError()
        if learnable_steer90:
            self.writer = SummaryWriter(ckpt_save_path + f'/logdir/{experiment_id}_{model_name}_learnable_steer90_' + time.strftime("%Y_%m_%d-%H_%M_%S"))
        elif steer90:
            self.writer = SummaryWriter(ckpt_save_path + f'/logdir/{experiment_id}_{model_name}_steer90_' + time.strftime("%Y_%m_%d-%H_%M_%S"))
        else:
            self.writer = SummaryWriter(ckpt_save_path + f'/logdir/{experiment_id}_{model_name}_' + time.strftime("%Y_%m_%d-%H_%M_%S"))
        self.model_name = model_name

        self.steer90 = steer90
        self.learnable_steer90 = learnable_steer90
        if self.steer90 or learnable_steer90:
            self.kpts_permutation = {}  # permutation for local kpt grid when the image is rotated
            for k in [1, 2, 3]:
                self.kpts_permutation[k] = torch.arange(64).reshape(8, 8).rot90(k).reshape(64)
                self.kpts_permutation[k] = torch.cat([self.kpts_permutation[k], torch.tensor([64])])  # dustbin
        if self.steer90:
            self.steer_permutation = {}  # permutation for features when the image is rotated
            for k in [1, 2, 3]:
                self.steer_permutation[k] = torch.arange(64).reshape(4, 16).roll(k, dims=0).reshape(64)
        elif self.learnable_steer90:
            self.steerer = nn.Conv2d(64, 64, kernel_size=1, padding=0, stride=1, bias=False).to(self.dev)

        # SETUP OPTIMIZER
        if self.train_only_descriptor:
            for x in self.net.keypoint_head.parameters():
                x.requires_grad = False
        if self.learnable_steer90:
            self.opt = optim.Adam([*self.steerer.parameters()] + [x for x in self.net.parameters() if x.requires_grad], lr = lr)
        else:
            self.opt = optim.Adam([x for x in self.net.parameters() if x.requires_grad], lr = lr)
        self.scheduler = torch.optim.lr_scheduler.StepLR(self.opt, step_size=30_000, gamma=gamma_steplr)


    def train(self):
        self.net.train()

        difficulty = 0.10

        p1s, p2s, H1, H2 = None, None, None, None
        d = None

        if self.augmentor is not None:
            p1s, p2s, H1, H2 = make_batch(self.augmentor, difficulty)
        
        if self.data_iter is not None:
            d = next(self.data_iter)

        with tqdm.tqdm(total=self.steps) as pbar:
            for i in range(self.steps):
                if not self.dry_run:
                    if self.data_iter is not None:
                        try:
                            # Get the next MD batch
                            d = next(self.data_iter)

                        except StopIteration:
                            print("End of DATASET!")
                            # If StopIteration is raised, create a new iterator.
                            self.data_iter = iter(self.data_loader)
                            d = next(self.data_iter)

                    if self.augmentor is not None:
                        #Grab synthetic data
                        p1s, p2s, H1, H2 = make_batch(self.augmentor, difficulty)

                if d is not None:
                    for k in d.keys():
                        if isinstance(d[k], torch.Tensor):
                            d[k] = d[k].to(self.dev)
                
                    p1, p2 = d['image0'], d['image1']
                    positives_md_coarse = megadepth_warper.spvs_coarse(d, 8)

                if self.augmentor is not None:
                    h_coarse, w_coarse = p1s[0].shape[-2] // 8, p1s[0].shape[-1] // 8
                    _ , positives_s_coarse = get_corresponding_pts(p1s, p2s, H1, H2, self.augmentor, h_coarse, w_coarse)

                #Join megadepth & synthetic data
                with torch.inference_mode():
                    #RGB -> GRAY
                    if d is not None:
                        p1 = p1.mean(1, keepdim=True)
                        p2 = p2.mean(1, keepdim=True)
                    if self.augmentor is not None:
                        p1s = p1s.mean(1, keepdim=True)
                        p2s = p2s.mean(1, keepdim=True)

                    #Cat two batches
                    if self.model_name in ('xfeat_default'):
                        p1 = torch.cat([p1s, p1], dim=0)
                        p2 = torch.cat([p2s, p2], dim=0)
                        positives_c = positives_s_coarse + positives_md_coarse
                    elif self.model_name in ('xfeat_synthetic'):
                        p1 = p1s ; p2 = p2s
                        positives_c = positives_s_coarse
                    else:
                        positives_c = positives_md_coarse

                #Check if batch is corrupted with too few correspondences
                is_corrupted = False
                for p in positives_c:
                    if len(p) < 30:
                        is_corrupted = True

                if is_corrupted:
                    continue

                #Forward pass
                if self.steer90 or self.learnable_steer90:
                    rot1 = np.random.randint(4)
                    rot2 = np.random.randint(4)
                    # rot_2to1 = (rot1 - rot2) % 4
                    # extract from rotated images
                    p1_rot = p1.rot90(k=rot1, dims=(-2, -1))
                    p2_rot = p2.rot90(k=rot2, dims=(-2, -1))
                    feats1, kpts1, hmap1 = self.net(p1_rot)
                    feats2, kpts2, hmap2 = self.net(p2_rot)
                    # rotate back extractions
                    if rot1 > 0:
                        hmap1 = hmap1.rot90(k=-rot1, dims=(-2, -1))
                        kpts1 = kpts1.rot90(k=-rot1, dims=(-2, -1))
                        kpts1 = kpts1[:, self.kpts_permutation[4-rot1]]  # this rotates the predicted fine keypoint grid
                        feats1 = feats1.rot90(k=-rot1, dims=(-2, -1))
                        if self.steer90:
                            feats1 = feats1[:, self.steer_permutation[4-rot1]]  # this steers the features in feature space
                        elif self.learnable_steer90:
                            for _ in range(4-rot1):
                                feats1 = self.steerer(feats1)
                    if rot2 > 0:
                        hmap2 = hmap2.rot90(k=-rot2, dims=(-2, -1))
                        kpts2 = kpts2.rot90(k=-rot2, dims=(-2, -1))
                        kpts2 = kpts2[:, self.kpts_permutation[4-rot2]]
                        feats2 = feats2.rot90(k=-rot2, dims=(-2, -1))
                        if self.steer90:
                            feats2 = feats2[:, self.steer_permutation[4-rot2]]
                        elif self.learnable_steer90:
                            for _ in range(4-rot2):
                                feats2 = self.steerer(feats2)
                    # steer feats2 to compensate for relative rotation
                    # if rot_2to1 > 0:
                    #     feats2 = feats2[:, self.steer_permutation[rot_2to1]]
                else:
                    feats1, kpts1, hmap1 = self.net(p1)
                    feats2, kpts2, hmap2 = self.net(p2)

                loss_items = []

                for b in range(len(positives_c)):
                    #Get positive correspondencies
                    pts1, pts2 = positives_c[b][:, :2], positives_c[b][:, 2:]

                    #Grab features at corresponding idxs
                    m1 = feats1[b, :, pts1[:,1].long(), pts1[:,0].long()].permute(1,0)
                    m2 = feats2[b, :, pts2[:,1].long(), pts2[:,0].long()].permute(1,0)

                    #grab heatmaps at corresponding idxs
                    h1 = hmap1[b, 0, pts1[:,1].long(), pts1[:,0].long()]
                    h2 = hmap2[b, 0, pts2[:,1].long(), pts2[:,0].long()]

                    coords1 = self.net.fine_matcher(torch.cat([m1, m2], dim=-1))

                    #Compute losses
                    loss_ds, conf = dual_softmax_loss(m1, m2)
                    loss_coords, acc_coords = coordinate_classification_loss(coords1, pts1, pts2, conf)

                    if not self.train_only_descriptor:
                        loss_kp_pos1, acc_pos1 = alike_distill_loss(kpts1[b], p1[b])
                        loss_kp_pos2, acc_pos2 = alike_distill_loss(kpts2[b], p2[b])
                        loss_kp_pos = (loss_kp_pos1 + loss_kp_pos2)*2.0
                        acc_pos = (acc_pos1 + acc_pos2)/2

                    loss_kp =  keypoint_loss(h1, conf) + keypoint_loss(h2, conf)

                    loss_items.append(loss_ds.unsqueeze(0))
                    loss_items.append(loss_coords.unsqueeze(0))
                    loss_items.append(loss_kp.unsqueeze(0))
                    if not self.train_only_descriptor:
                        loss_items.append(loss_kp_pos.unsqueeze(0))

                    if b == 0:
                        acc_coarse_0 = check_accuracy(m1, m2)

                acc_coarse = check_accuracy(m1, m2)

                nb_coarse = len(m1)
                loss = torch.cat(loss_items, -1).mean()
                loss_coarse = loss_ds.item()
                loss_coord = loss_coords.item()
                loss_coord = loss_coords.item()
                if not self.train_only_descriptor:
                    loss_kp_pos = loss_kp_pos.item()
                loss_l1 = loss_kp.item()

                # Compute Backward Pass
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.net.parameters(), 1.)
                self.opt.step()
                self.opt.zero_grad()
                self.scheduler.step()

                if (i+1) % self.save_ckpt_every == 0:
                    print('saving iter ', i+1)
                    torch.save(self.net.state_dict(), self.ckpt_save_path + f'/{self.model_name}_{i+1}.pth')
                    if self.learnable_steer90:
                        torch.save(self.steerer.state_dict(), self.ckpt_save_path + f'/steerer_{self.model_name}_{i+1}.pth')

                if self.train_only_descriptor:
                    loss_kp_pos = np.nan
                    acc_pos = np.nan
                pbar.set_description( 'Loss: {:.4f} acc_c0 {:.3f} acc_c1 {:.3f} acc_f: {:.3f} loss_c: {:.3f} loss_f: {:.3f} loss_kp: {:.3f} #matches_c: {:d} loss_kp_pos: {:.3f} acc_kp_pos: {:.3f}'.format(
                                                                        loss.item(), acc_coarse_0, acc_coarse, acc_coords, loss_coarse, loss_coord, loss_l1, nb_coarse, loss_kp_pos, acc_pos) )
                pbar.update(1)

                # Log metrics
                self.writer.add_scalar('Loss/total', loss.item(), i)
                self.writer.add_scalar('Accuracy/coarse_synth', acc_coarse_0, i)
                self.writer.add_scalar('Accuracy/coarse_mdepth', acc_coarse, i)
                self.writer.add_scalar('Accuracy/fine_mdepth', acc_coords, i)
                self.writer.add_scalar('Accuracy/kp_position', acc_pos, i)
                self.writer.add_scalar('Loss/coarse', loss_coarse, i)
                self.writer.add_scalar('Loss/fine', loss_coord, i)
                self.writer.add_scalar('Loss/reliability', loss_l1, i)
                self.writer.add_scalar('Loss/keypoint_pos', loss_kp_pos, i)
                self.writer.add_scalar('Count/matches_coarse', nb_coarse, i)



if __name__ == '__main__':

    trainer = Trainer(
        megadepth_root_path=args.megadepth_root_path, 
        synthetic_root_path=args.synthetic_root_path, 
        ckpt_save_path=args.ckpt_save_path,
        experiment_id=args.experiment_id,
        model_name=args.training_type,
        batch_size=args.batch_size,
        n_steps=args.n_steps,
        lr=args.lr,
        gamma_steplr=args.gamma_steplr,
        steer90=args.steer90,
        learnable_steer90=args.learnable_steer90,
        training_res=args.training_res,
        device_num=args.device_num,
        dry_run=args.dry_run,
        save_ckpt_every=args.save_ckpt_every,
        num_workers=args.num_workers,
        pretrained_weights=args.pretrained_weights,
        train_only_descriptor=args.train_only_descriptor,
    )

    #The most fun part
    trainer.train()
