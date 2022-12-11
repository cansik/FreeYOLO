import torch
import torch.nn as nn

from ..backbone import build_backbone
from ..neck import build_neck, build_fpn
from ..head import build_head

from utils.nms import multiclass_nms


# Anchor-free YOLO
class FreeYOLO(nn.Module):
    def __init__(self, 
                 cfg,
                 device, 
                 num_classes = 20, 
                 conf_thresh = 0.05,
                 nms_thresh = 0.6,
                 trainable = False, 
                 topk = 1000,
                 no_decode = False):
        super(FreeYOLO, self).__init__()
        # --------- Basic Parameters ----------
        self.cfg = cfg
        self.device = device
        self.stride = cfg['stride']
        self.num_classes = num_classes
        self.trainable = trainable
        self.conf_thresh = conf_thresh
        self.nms_thresh = nms_thresh
        self.topk = topk
        self.no_decode = no_decode
        
        # --------- Network Parameters ----------
        ## backbone
        self.backbone, bk_dim = build_backbone(cfg=cfg, trainable=trainable)

        ## neck
        self.neck = build_neck(cfg=cfg, in_dim=bk_dim[-1], out_dim=cfg['neck_dim'])
        
        ## fpn
        self.fpn = build_fpn(cfg=cfg, in_dims=cfg['fpn_dim'], out_dim=cfg['head_dim'])

        ## non-shared heads
        self.non_shared_heads = nn.ModuleList(
            [build_head(cfg) 
            for _ in range(len(cfg['stride']))
            ])

        ## pred
        head_dim = cfg['head_dim']
        self.obj_preds = nn.ModuleList(
                            [nn.Conv2d(head_dim, 1, kernel_size=1) 
                              for _ in range(len(cfg['stride']))
                              ])
        self.cls_preds = nn.ModuleList(
                            [nn.Conv2d(head_dim, self.num_classes, kernel_size=1) 
                              for _ in range(len(cfg['stride']))
                              ]) 
        self.reg_preds = nn.ModuleList(
                            [nn.Conv2d(head_dim, 4, kernel_size=1) 
                              for _ in range(len(cfg['stride']))
                              ])                 

        # --------- Network Initialization ----------
        # init bias
        self.init_yolo()


    def init_yolo(self): 
        # Init yolo
        for m in self.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.eps = 1e-3
                m.momentum = 0.03
                
        # Init bias
        init_prob = 0.01
        bias_value = -torch.log(torch.tensor((1. - init_prob) / init_prob))
        # obj pred
        for obj_pred in self.obj_preds:
            b = obj_pred.bias.view(1, -1)
            b.data.fill_(bias_value.item())
            obj_pred.bias = torch.nn.Parameter(b.view(-1), requires_grad=True)
        # cls pred
        for cls_pred in self.cls_preds:
            b = cls_pred.bias.view(1, -1)
            b.data.fill_(bias_value.item())
            cls_pred.bias = torch.nn.Parameter(b.view(-1), requires_grad=True)


    def generate_anchors(self, level, fmp_size):
        """
            fmp_size: (List) [H, W]
        """
        # generate grid cells
        fmp_h, fmp_w = fmp_size
        anchor_y, anchor_x = torch.meshgrid([torch.arange(fmp_h), torch.arange(fmp_w)])
        # [H, W, 2] -> [HW, 2]
        anchor_xy = torch.stack([anchor_x, anchor_y], dim=-1).float().view(-1, 2) + 0.5
        anchor_xy *= self.stride[level]
        anchors = anchor_xy.to(self.device)

        return anchors
        

    def decode_boxes(self, anchors, pred_regs, stride):
        """
            anchors:  (List[Tensor]) [1, M, 2] or [M, 2]
            pred_reg: (List[Tensor]) [B, M, 4] or [M, 4]
        """
        # center of bbox
        pred_ctr_xy = anchors + pred_regs[..., :2] * stride
        # size of bbox
        pred_box_wh = pred_regs[..., 2:].exp() * stride

        pred_x1y1 = pred_ctr_xy - 0.5 * pred_box_wh
        pred_x2y2 = pred_ctr_xy + 0.5 * pred_box_wh
        pred_box = torch.cat([pred_x1y1, pred_x2y2], dim=-1)

        return pred_box


    def post_process(self, obj_preds, cls_preds, reg_preds, anchors):
        """
        Input:
            obj_preds: List(Tensor) [[H x W, 1], ...]
            cls_preds: List(Tensor) [[H x W, C], ...]
            reg_preds: List(Tensor) [[H x W, 4], ...]
            anchors:  List(Tensor) [[H x W, 2], ...]
        """
        all_scores = []
        all_labels = []
        all_bboxes = []
        
        for level, (obj_pred_i, cls_pred_i, reg_pred_i, anchors_i) in enumerate(zip(obj_preds, cls_preds, reg_preds, anchors)):
            # (H x W x C,)
            scores_i = (torch.sqrt(obj_pred_i.sigmoid() * cls_pred_i.sigmoid())).flatten()

            # Keep top k top scoring indices only.
            num_topk = min(self.topk, reg_pred_i.size(0))

            # torch.sort is actually faster than .topk (at least on GPUs)
            predicted_prob, topk_idxs = scores_i.sort(descending=True)
            topk_scores = predicted_prob[:num_topk]
            topk_idxs = topk_idxs[:num_topk]

            # print(topk_scores, topk_scores.shape, self.num_classes)
            # exit()

            # filter out the proposals with low confidence score
            keep_idxs = topk_scores > self.conf_thresh
            scores = topk_scores[keep_idxs]
            topk_idxs = topk_idxs[keep_idxs]

            anchor_idxs = torch.div(topk_idxs, self.num_classes, rounding_mode='floor')
            labels = topk_idxs % self.num_classes

            reg_pred_i = reg_pred_i[anchor_idxs]
            anchors_i = anchors_i[anchor_idxs]

            # decode box: [M, 4]
            bboxes = self.decode_boxes(anchors_i, reg_pred_i, self.stride[level])

            all_scores.append(scores)
            all_labels.append(labels)
            all_bboxes.append(bboxes)

        scores = torch.cat(all_scores)
        labels = torch.cat(all_labels)
        bboxes = torch.cat(all_bboxes)

        # to cpu & numpy
        scores = scores.cpu().numpy()
        labels = labels.cpu().numpy()
        bboxes = bboxes.cpu().numpy()

        # nms
        scores, labels, bboxes = multiclass_nms(
            scores, labels, bboxes, self.nms_thresh, self.num_classes, False)

        return bboxes, scores, labels


    @torch.no_grad()
    def inference_single_image(self, x):
        img_h, img_w = x.shape[2:]
        # backbone
        feats = self.backbone(x)

        # neck
        feats['layer4'] = self.neck(feats['layer4'])

        # fpn
        pyramid_feats = [feats['layer2'], feats['layer3'], feats['layer4']]
        pyramid_feats = self.fpn(pyramid_feats)

        # non-shared heads
        all_obj_preds = []
        all_cls_preds = []
        all_reg_preds = []
        all_anchors = []
        for level, (feat, head) in enumerate(zip(pyramid_feats, self.non_shared_heads)):
            cls_feat, reg_feat = head(feat)

            # [1, C, H, W]
            obj_pred = self.obj_preds[level](reg_feat)
            cls_pred = self.cls_preds[level](cls_feat)
            reg_pred = self.reg_preds[level](reg_feat)

            if self.no_decode:
                anchors = None
            else:
                _, _, H, W = cls_pred.size()
                fmp_size = [H, W]
                # [M, 4]
                anchors = self.generate_anchors(level, fmp_size)

            # [1, C, H, W] -> [H, W, C] -> [M, C]
            obj_pred = obj_pred[0].permute(1, 2, 0).contiguous().view(-1, 1)
            cls_pred = cls_pred[0].permute(1, 2, 0).contiguous().view(-1, self.num_classes)
            reg_pred = reg_pred[0].permute(1, 2, 0).contiguous().view(-1, 4)

            all_obj_preds.append(obj_pred)
            all_cls_preds.append(cls_pred)
            all_reg_preds.append(reg_pred)
            all_anchors.append(anchors)

        if self.no_decode:
            # no post process
            obj_preds = torch.cat(all_obj_preds, dim=0)
            cls_preds = torch.cat(all_cls_preds, dim=0)
            reg_preds = torch.cat(all_reg_preds, dim=0)
            # [n_anchors_all, 4 + 1 + C]
            outputs = torch.cat([reg_preds, obj_preds.sigmoid(), cls_preds.sigmoid()], dim=-1)

            return outputs

        else:
            # post process
            bboxes, scores, labels = self.post_process(all_obj_preds, all_cls_preds, all_reg_preds, all_anchors)
            
            # normalize bbox
            bboxes /= max(img_h, img_w)
            bboxes = bboxes.clip(0., 1.)

            return bboxes, scores, labels


    def forward(self, x):
        if not self.trainable:
            return self.inference_single_image(x)
        else:
            # backbone
            feats = self.backbone(x)

            # neck
            feats['layer4'] = self.neck(feats['layer4'])

            # fpn
            pyramid_feats = [feats['layer2'], feats['layer3'], feats['layer4']]
            pyramid_feats = self.fpn(pyramid_feats)

            # non-shared heads
            all_anchors = []
            all_obj_preds = []
            all_cls_preds = []
            all_box_preds = []
            for level, (feat, head) in enumerate(zip(pyramid_feats, self.non_shared_heads)):
                cls_feat, reg_feat = head(feat)

                # [1, C, H, W]
                obj_pred = self.obj_preds[level](reg_feat)
                cls_pred = self.cls_preds[level](cls_feat)
                reg_pred = self.reg_preds[level](reg_feat)

                B, _, H, W = cls_pred.size()
                fmp_size = [H, W]
                # generate anchor boxes: [M, 4]
                anchors = self.generate_anchors(level, fmp_size)
                
                # [B, C, H, W] -> [B, H, W, C] -> [B, M, C]
                obj_pred = obj_pred.permute(0, 2, 3, 1).contiguous().view(B, -1, 1)
                cls_pred = cls_pred.permute(0, 2, 3, 1).contiguous().view(B, -1, self.num_classes)
                reg_pred = reg_pred.permute(0, 2, 3, 1).contiguous().view(B, -1, 4)

                # decode box: [M, 4]
                box_pred = self.decode_boxes(anchors, reg_pred, self.stride[level])

                all_obj_preds.append(obj_pred)
                all_cls_preds.append(cls_pred)
                all_box_preds.append(box_pred)
                all_anchors.append(anchors)
            
            # output dict
            outputs = {"pred_obj": all_obj_preds,        # List(Tensor) [B, M, 1]
                       "pred_cls": all_cls_preds,        # List(Tensor) [B, M, C]
                       "pred_box": all_box_preds,        # List(Tensor) [B, M, 4]
                       "anchors": all_anchors,           # List(Tensor) [B, M, 2]
                       'strides': self.stride}           # List(Int) [8, 16, 32]

            return outputs 
    