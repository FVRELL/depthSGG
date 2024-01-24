# ------------------------------------------------------------------------------
# Portions of this code are from
# CornerNet (https://github.com/princeton-vl/CornerNet)
# Copyright (c) 2018, University of Michigan
# Licensed under the BSD 3-Clause License
# ------------------------------------------------------------------------------
import torch
import torch.nn as nn
import torch.nn.functional as F
from .utils import generalized_box_iou, _ciou


def _gather_feat(feat, ind, mask=None):
    dim = feat.size(2)
    ind = ind.unsqueeze(2).expand(ind.size(0), ind.size(1), dim)
    feat = feat.gather(1, ind)
    if mask is not None:
        mask = mask.unsqueeze(2).expand_as(feat)
        feat = feat[mask]
        feat = feat.view(-1, dim)
    return feat


def _transpose_and_gather_feat(feat, ind):
    feat = feat.permute(0, 2, 3, 1).contiguous()
    feat = feat.view(feat.size(0), -1, feat.size(3))
    feat = _gather_feat(feat, ind)
    return feat


def _transpose_and_gather_feat_5D(feat, ind):
    feat = feat.permute(0, 1, 3, 4, 2).contiguous()
    feat = feat.view(feat.size(0), -1, feat.size(4))
    feat = _gather_feat(feat, ind)
    return feat


def _slow_neg_loss(pred, gt):
    '''focal loss from CornerNet'''
    pos_inds = gt.eq(1)
    neg_inds = gt.lt(1)

    neg_weights = torch.pow(1 - gt[neg_inds], 4)

    loss = 0
    pos_pred = pred[pos_inds]
    neg_pred = pred[neg_inds]

    pos_loss = torch.log(pos_pred) * torch.pow(1 - pos_pred, 2)
    neg_loss = torch.log(1 - neg_pred) * torch.pow(neg_pred, 2) * neg_weights

    num_pos = pos_inds.float().sum()
    pos_loss = pos_loss.sum()
    neg_loss = neg_loss.sum()

    if pos_pred.nelement() == 0:
        loss = loss - neg_loss
    else:
        loss = loss - (pos_loss + neg_loss) / num_pos
    return loss


def _neg_loss(pred, gt):
    ''' Modified focal loss. Exactly the same as CornerNet.
      Runs faster and costs a little bit more memory
    Arguments:
      pred (batch x c x h x w)
      gt_regr (batch x c x h x w)
  '''
    pos_inds = gt.eq(1).float()
    neg_inds = gt.lt(1).float()

    neg_weights = torch.pow(1 - gt, 4)
    # print(f"neg_weights is {torch.all(torch.isfinite(neg_weights))}")
    # print(f"pos_weight is {torch.all(torch.isfinite(torch.pow(1 - pred, 2)))}")
    loss = 0
    # print(f'any pred is 0 {torch.any(pred==0)}')
    # print(f'any pred is 1 {torch.any(pred==1)}')
    # print(f'any log nan is {torch.all(torch.isfinite(torch.log(pred)))}')
    pos_loss = torch.log(pred+1e-10) * torch.pow(1 - pred, 2) * pos_inds
    neg_loss = torch.log(1 - pred+1e-10) * torch.pow(pred, 2) * neg_weights * neg_inds

    num_pos = pos_inds.float().sum()
    # print(f'pose lose is nan is {torch.all(torch.isfinite(pos_loss))}')
    pos_loss = pos_loss.sum()
    # print(f"pose sum loss is nan is {torch.isfinite(pos_loss)}")
    neg_loss = neg_loss.sum()
    # print(f"num pos is {num_pos}")
    if num_pos == 0:
        loss = loss - neg_loss
    else:
        loss = loss - (pos_loss + neg_loss) / num_pos
    # print(f"pos loss is nan {torch.isfinite(pos_loss)}")
    # print(f"neg loss is nan{torch.isfinite(neg_loss)}")
    # print(f"total loss is nan {torch.isfinite(loss)}")
    return loss


def _not_faster_neg_loss(pred, gt):
    pos_inds = gt.eq(1).float()
    neg_inds = gt.lt(1).float()
    num_pos = pos_inds.float().sum()
    neg_weights = torch.pow(1 - gt, 4)

    loss = 0
    trans_pred = pred * neg_inds + (1 - pred) * pos_inds
    weight = neg_weights * neg_inds + pos_inds
    all_loss = torch.log(1 - trans_pred) * torch.pow(trans_pred, 2) * weight
    all_loss = all_loss.sum()

    if num_pos > 0:
        all_loss /= num_pos
    loss -= all_loss
    return loss


def _slow_reg_loss(regr, gt_regr, mask):
    num = mask.float().sum()
    mask = mask.unsqueeze(2).expand_as(gt_regr)

    regr = regr[mask]
    gt_regr = gt_regr[mask]

    regr_loss = nn.functional.smooth_l1_loss(regr, gt_regr, reduction='sum')
    regr_loss = regr_loss / (num + 1e-4)
    return regr_loss


def _reg_loss(regr, gt_regr, mask):
    ''' L1 regression loss
    Arguments:
      regr (batch x max_objects x dim)
      gt_regr (batch x max_objects x dim)
      mask (batch x max_objects)
  '''
    num = mask.float().sum()
    mask = mask.unsqueeze(2).expand_as(gt_regr).float()

    regr = regr * mask
    gt_regr = gt_regr * mask

    regr_loss = nn.functional.smooth_l1_loss(regr, gt_regr, reduction='sum')
    regr_loss = regr_loss / (num + 1e-4)
    return regr_loss


class FocalLoss(nn.Module):
    '''nn.Module warpper for focal loss'''

    def __init__(self):
        super(FocalLoss, self).__init__()
        self.neg_loss = _neg_loss

    def forward(self, out, target):
        return self.neg_loss(out, target)


class RegLoss(nn.Module):
    '''Regression loss for an output tensor
    Arguments:
      output (batch x dim x h x w)
      mask (batch x max_objects)
      ind (batch x max_objects)
      target (batch x max_objects x dim)
  '''

    def __init__(self):
        super(RegLoss, self).__init__()

    def forward(self, output, mask, ind, target):
        pred = _transpose_and_gather_feat(output, ind)
        loss = _reg_loss(pred, target, mask)
        return loss


class RegL1Loss(nn.Module):
    def __init__(self):
        super(RegL1Loss, self).__init__()

    def forward(self, output, mask, ind, target):
        pred = _transpose_and_gather_feat(output, ind)
        mask = mask.unsqueeze(2).expand_as(pred).float()
        # loss = F.l1_loss(pred * mask, target * mask, reduction='elementwise_mean')
        loss = F.l1_loss(pred * mask, target * mask, reduction='sum')
        loss = loss / (mask.sum() + 1e-4)
        return loss


class GIOULoss(nn.Module):
    def __init__(self):
        super(GIOULoss, self).__init__()

    def forward(self, output_wh, output_reg, mask, ind, target_wh, target_reg):
        pred_wh = _transpose_and_gather_feat(output_wh, ind)
        pred_reg = _transpose_and_gather_feat(output_reg, ind)
        wh = output_wh.shape
        x_indices = ind % wh[2]
        y_indices = (ind - x_indices) / wh[2]
        indices = torch.cat([x_indices.unsqueeze(-1), y_indices.unsqueeze(-1)], dim=2)
        pred = indices + pred_reg
        target = indices + target_reg
        pred = torch.cat([pred - pred_wh / 2.0, pred + pred_wh / 2.0], dim=2).reshape(-1, 4)
        target = torch.cat([target - target_wh / 2.0, target + target_wh / 2.0], dim=2).reshape(-1, 4)
        mask = mask.reshape(-1)
        # loss = F.l1_loss(pred * mask, target * mask, reduction='elementwise_mean')
        loss = (1 - torch.diag(generalized_box_iou(pred[mask > 0, :], target[mask > 0, :]))).sum()
        loss = loss / (mask.sum() + 1e-4)
        return loss


class CIOULoss(nn.Module):
    def __init__(self):
        super(CIOULoss, self).__init__()

    def forward(self, output_wh, output_reg, mask, ind, target_wh, target_reg):
        pred_wh = _transpose_and_gather_feat(output_wh, ind)
        pred_reg = _transpose_and_gather_feat(output_reg, ind)
        wh = output_wh.shape
        x_indices = ind % wh[2]
        y_indices = (ind - x_indices) / wh[2]
        indices = torch.cat([x_indices.unsqueeze(-1), y_indices.unsqueeze(-1)], dim=2)
        pred = indices + pred_reg
        target = indices + target_reg
        pred = torch.cat([pred, pred_wh], dim=2).reshape(-1, 4)
        target = torch.cat([target, target_wh / 2.0], dim=2).reshape(-1, 4)
        mask = mask.reshape(-1)
        # loss = F.l1_loss(pred * mask, target * mask, reduction='elementwise_mean')
        loss = (1 - _ciou(pred[mask > 0, :], target[mask > 0, :])).sum()
        loss = loss / (mask.sum() + 1e-4)
        return loss


class RegL1Loss_5D(nn.Module):
    def __init__(self):
        super(RegL1Loss_5D, self).__init__()

    def forward(self, output, mask, ind, target):
        pred = _transpose_and_gather_feat_5D(output, ind)
        mask = mask.unsqueeze(2).expand_as(pred).float()
        # loss = F.l1_loss(pred * mask, target * mask, reduction='elementwise_mean')
        loss = F.l1_loss(pred * mask, target * mask, reduction='sum')
        loss = loss / (mask.sum() + 1e-4)
        return loss


class NormRegL1Loss(nn.Module):
    def __init__(self):
        super(NormRegL1Loss, self).__init__()

    def forward(self, output, mask, ind, target):
        pred = _transpose_and_gather_feat(output, ind)
        mask = mask.unsqueeze(2).expand_as(pred).float()
        # loss = F.l1_loss(pred * mask, target * mask, reduction='elementwise_mean')
        pred = pred / (target + 1e-4)
        target = target * 0 + 1
        loss = F.l1_loss(pred * mask, target * mask, reduction='sum')
        loss = loss / (mask.sum() + 1e-4)
        return loss


class RegWeightedL1Loss(nn.Module):
    def __init__(self):
        super(RegWeightedL1Loss, self).__init__()

    def forward(self, output, mask, ind, target):
        pred = _transpose_and_gather_feat(output, ind)
        mask = mask.float()
        # loss = F.l1_loss(pred * mask, target * mask, reduction='elementwise_mean')
        loss = F.l1_loss(pred * mask, target * mask, reduction='sum')
        loss = loss / (mask.sum() + 1e-4)
        return loss


class L1Loss(nn.Module):
    def __init__(self):
        super(L1Loss, self).__init__()

    def forward(self, output, mask, ind, target):
        pred = _transpose_and_gather_feat(output, ind)
        mask = mask.unsqueeze(2).expand_as(pred).float()
        loss = F.l1_loss(pred * mask, target * mask, reduction='elementwise_mean')
        return loss


class BinRotLoss(nn.Module):
    def __init__(self):
        super(BinRotLoss, self).__init__()

    def forward(self, output, mask, ind, rotbin, rotres):
        pred = _transpose_and_gather_feat(output, ind)
        loss = compute_rot_loss(pred, rotbin, rotres, mask)
        return loss


def compute_res_loss(output, target):
    return F.smooth_l1_loss(output, target, reduction='elementwise_mean')


# TODO: weight
def compute_bin_loss(output, target, mask):
    mask = mask.expand_as(output)
    output = output * mask.float()
    return F.cross_entropy(output, target, reduction='elementwise_mean')


def compute_rot_loss(output, target_bin, target_res, mask):
    # output: (B, 128, 8) [bin1_cls[0], bin1_cls[1], bin1_sin, bin1_cos,
    #                 bin2_cls[0], bin2_cls[1], bin2_sin, bin2_cos]
    # target_bin: (B, 128, 2) [bin1_cls, bin2_cls]
    # target_res: (B, 128, 2) [bin1_res, bin2_res]
    # mask: (B, 128, 1)
    # import pdb; pdb.set_trace()
    output = output.view(-1, 8)
    target_bin = target_bin.view(-1, 2)
    target_res = target_res.view(-1, 2)
    mask = mask.view(-1, 1)
    loss_bin1 = compute_bin_loss(output[:, 0:2], target_bin[:, 0], mask)
    loss_bin2 = compute_bin_loss(output[:, 4:6], target_bin[:, 1], mask)
    loss_res = torch.zeros_like(loss_bin1)
    if target_bin[:, 0].nonzero().shape[0] > 0:
        idx1 = target_bin[:, 0].nonzero()[:, 0]
        valid_output1 = torch.index_select(output, 0, idx1.long())
        valid_target_res1 = torch.index_select(target_res, 0, idx1.long())
        loss_sin1 = compute_res_loss(
            valid_output1[:, 2], torch.sin(valid_target_res1[:, 0]))
        loss_cos1 = compute_res_loss(
            valid_output1[:, 3], torch.cos(valid_target_res1[:, 0]))
        loss_res += loss_sin1 + loss_cos1
    if target_bin[:, 1].nonzero().shape[0] > 0:
        idx2 = target_bin[:, 1].nonzero()[:, 0]
        valid_output2 = torch.index_select(output, 0, idx2.long())
        valid_target_res2 = torch.index_select(target_res, 0, idx2.long())
        loss_sin2 = compute_res_loss(
            valid_output2[:, 6], torch.sin(valid_target_res2[:, 1]))
        loss_cos2 = compute_res_loss(
            valid_output2[:, 7], torch.cos(valid_target_res2[:, 1]))
        loss_res += loss_sin2 + loss_cos2
    return loss_bin1 + loss_bin2 + loss_res
