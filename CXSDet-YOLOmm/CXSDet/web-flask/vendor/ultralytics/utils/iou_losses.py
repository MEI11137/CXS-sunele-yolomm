# Ultralytics AGPL-3.0 License - https://ultralytics.com/license

"""
Extended IoU Loss Functions for YOLOMM

This module implements advanced IoU-based loss functions:
- SIoU (SCYLLA-IoU): https://arxiv.org/abs/2205.12740
- EIoU (Efficient-IoU): https://arxiv.org/abs/2101.08158
- WIoU (Wise-IoU): https://arxiv.org/abs/2301.10051
- Alpha-IoU: https://arxiv.org/abs/2110.13675
"""

import math
import torch
import torch.nn as nn
import torch.nn.functional as F


class SIoULoss(nn.Module):
    """
    SIoU (SCYLLA-IoU) similarity score, consumed as 1 - score.

    SIoU considers angle costs, distance costs, and shape costs to improve
    bounding box regression accuracy. It helps the model converge faster
    by focusing on the regression of the nearest axis.

    Paper: https://arxiv.org/abs/2205.12740

    Args:
        angle_alpha (float): Shape exponent theta (legacy parameter name). Default: 4.0
        dist_beta (float): Exponent for distance cost. Default: 1.0
        gamma (float): Weight for shape cost. Default: 0.5
        eps (float): Small value for numerical stability. Default: 1e-7
    """

    def __init__(
        self,
        angle_alpha: float = 4.0,
        dist_beta: float = 1.0,
        gamma: float = 0.5,
        eps: float = 1e-7,
    ):
        super().__init__()
        self.angle_alpha = angle_alpha
        self.dist_beta = dist_beta
        self.gamma = gamma
        self.eps = eps

    def forward(
        self,
        pred_boxes: torch.Tensor,
        target_boxes: torch.Tensor,
        xywh: bool = True,
    ) -> torch.Tensor:
        """
        Calculate SIoU loss.

        Args:
            pred_boxes (torch.Tensor): Predicted boxes, shape (N, 4)
            target_boxes (torch.Tensor): Target boxes, shape (N, 4)
            xywh (bool): If True, boxes are in (x, y, w, h) format, else (x1, y1, x2, y2)

        Returns:
            torch.Tensor: SIoU loss values, shape (N,)
        """
        # Convert to xyxy if needed
        if xywh:
            px, py, pw, ph = pred_boxes.unbind(-1)
            tx, ty, tw, th = target_boxes.unbind(-1)
            px1 = px - pw / 2
            py1 = py - ph / 2
            px2 = px + pw / 2
            py2 = py + ph / 2
            tx1 = tx - tw / 2
            ty1 = ty - th / 2
            tx2 = tx + tw / 2
            ty2 = ty + th / 2
        else:
            px1, py1, px2, py2 = pred_boxes.unbind(-1)
            tx1, ty1, tx2, ty2 = target_boxes.unbind(-1)
            pw = px2 - px1
            ph = py2 - py1
            tw = tx2 - tx1
            th = ty2 - ty1

        # Intersection
        ix1 = torch.maximum(px1, tx1)
        iy1 = torch.maximum(py1, ty1)
        ix2 = torch.minimum(px2, tx2)
        iy2 = torch.minimum(py2, ty2)
        inter_w = (ix2 - ix1).clamp(0)
        inter_h = (iy2 - iy1).clamp(0)
        inter = inter_w * inter_h

        # Union
        pred_area = pw * ph
        target_area = tw * th
        union = pred_area + target_area - inter + self.eps

        # IoU
        iou = inter / union

        # Center points
        pcx = (px1 + px2) / 2
        pcy = (py1 + py2) / 2
        tcx = (tx1 + tx2) / 2
        tcy = (ty1 + ty2) / 2

        # SIoU's angle term modulates the distance cost; it is not an
        # independent penalty. Avoid atan2(0, 0) and its undefined gradient.
        delta_x = tcx - pcx
        delta_y = tcy - pcy
        angle_cost = 2 * delta_x.abs() * delta_y.abs() / (delta_x.square() + delta_y.square()).clamp_min(self.eps)
        distance_gamma = (2 - angle_cost).pow(self.dist_beta)

        # Distance cost (penalize center distance)
        ch = torch.maximum(py2, ty2) - torch.minimum(py1, ty1)
        cw = torch.maximum(px2, tx2) - torch.minimum(px1, tx1)

        # Distance to the bounding box center
        distance_x = (delta_x / cw.clamp_min(self.eps)) ** 2
        distance_y = (delta_y / ch.clamp_min(self.eps)) ** 2
        distance_cost = 2 - torch.exp(-distance_gamma * distance_x) - torch.exp(-distance_gamma * distance_y)

        # Shape cost (aspect ratio consistency)
        width_diff = (pw - tw).abs() / torch.maximum(pw, tw).clamp_min(self.eps)
        height_diff = (ph - th).abs() / torch.maximum(ph, th).clamp_min(self.eps)
        shape_loss = (1 - torch.exp(-width_diff)).pow(self.angle_alpha) + (1 - torch.exp(-height_diff)).pow(self.angle_alpha)

        # SIoU loss
        siou = iou - self.gamma * (distance_cost + shape_loss)
        return siou


class EIoULoss(nn.Module):
    """
    EIoU (Efficient-IoU) Loss.

    EIoU improves CIoU by separately penalizing width and height differences
    between predicted and target boxes, providing better guidance for box regression.

    Paper: https://arxiv.org/abs/2101.08158

    Args:
        rho (float): Weight for the overlap and center distance terms. Default: 1.0
        sigma (float): Weight for the width/height penalty terms. Default: 1.0
        eps (float): Small value for numerical stability. Default: 1e-7
    """

    def __init__(
        self,
        rho: float = 1.0,
        sigma: float = 1.0,
        eps: float = 1e-7,
    ):
        super().__init__()
        self.rho = rho
        self.sigma = sigma
        self.eps = eps

    def forward(
        self,
        pred_boxes: torch.Tensor,
        target_boxes: torch.Tensor,
        xywh: bool = True,
    ) -> torch.Tensor:
        """
        Calculate EIoU loss.

        Args:
            pred_boxes (torch.Tensor): Predicted boxes, shape (N, 4)
            target_boxes (torch.Tensor): Target boxes, shape (N, 4)
            xywh (bool): If True, boxes are in (x, y, w, h) format, else (x1, y1, x2, y2)

        Returns:
            torch.Tensor: EIoU loss values, shape (N,)
        """
        # Convert to xyxy if needed
        if xywh:
            px, py, pw, ph = pred_boxes.unbind(-1)
            tx, ty, tw, th = target_boxes.unbind(-1)
            px1 = px - pw / 2
            py1 = py - ph / 2
            px2 = px + pw / 2
            py2 = py + ph / 2
            tx1 = tx - tw / 2
            ty1 = ty - th / 2
            tx2 = tx + tw / 2
            ty2 = ty + th / 2
        else:
            px1, py1, px2, py2 = pred_boxes.unbind(-1)
            tx1, ty1, tx2, ty2 = target_boxes.unbind(-1)
            pw = px2 - px1
            ph = py2 - py1
            tw = tx2 - tx1
            th = ty2 - ty1

        # Intersection
        ix1 = torch.maximum(px1, tx1)
        iy1 = torch.maximum(py1, ty1)
        ix2 = torch.minimum(px2, tx2)
        iy2 = torch.minimum(py2, ty2)
        inter = (ix2 - ix1).clamp(0) * (iy2 - iy1).clamp(0)

        # Union
        union = pw * ph + tw * th - inter + self.eps

        # IoU
        iou = inter / union

        # Smallest enclosing box
        cx1 = torch.minimum(px1, tx1)
        cy1 = torch.minimum(py1, ty1)
        cx2 = torch.maximum(px2, tx2)
        cy2 = torch.maximum(py2, ty2)
        cw = cx2 - cx1
        ch = cy2 - cy1

        # Center distance
        pcx = (px1 + px2) / 2
        pcy = (py1 + py2) / 2
        tcx = (tx1 + tx2) / 2
        tcy = (ty1 + ty2) / 2

        # Distance cost (normalized by diagonal of enclosing box)
        center_dist = ((pcx - tcx) ** 2 + (pcy - tcy) ** 2) / (cw ** 2 + ch ** 2 + self.eps)

        # Width and height penalties
        width_penalty = (pw - tw) ** 2 / (cw ** 2 + self.eps)
        height_penalty = (ph - th) ** 2 / (ch ** 2 + self.eps)

        # EIoU
        eiou = iou - self.rho * center_dist - self.sigma * (width_penalty + height_penalty)

        # Negative scores carry distance/shape gradients for disjoint boxes.
        return eiou


class WIoULoss(nn.Module):
    """
    WIoU (Wise-IoU) Loss.

    WIoU uses a dynamic gradient gain strategy based on the quality of
    anchor boxes. It applies smaller gradients to high-quality anchor boxes
    to prevent them from dominating the gradient.

    Paper: https://arxiv.org/abs/2301.10051

    Args:
        v_threshold (float): Legacy compatibility value; v3 uses alpha/delta focusing instead.
        eps (float): Small value for numerical stability. Default: 1e-6
    """

    def __init__(
        self,
        v_threshold: float = 0.7,
        eps: float = 1e-6,
        alpha: float = 1.9,
        delta: float = 3.0,
        momentum: float = 1 - 0.5 ** (1 / 7000),
    ):
        super().__init__()
        self.v_threshold = v_threshold
        self.eps = eps
        self.alpha = alpha
        self.delta = delta
        self.momentum = momentum
        self.register_buffer("iou_mean", torch.tensor(1.0))

    def forward(
        self,
        pred_boxes: torch.Tensor,
        target_boxes: torch.Tensor,
        xywh: bool = True,
    ) -> torch.Tensor:
        """
        Calculate WIoU loss (version 3).

        Args:
            pred_boxes (torch.Tensor): Predicted boxes, shape (N, 4)
            target_boxes (torch.Tensor): Target boxes, shape (N, 4)
            xywh (bool): If True, boxes are in (x, y, w, h) format, else (x1, y1, x2, y2)

        Returns:
            torch.Tensor: WIoU loss values, shape (N,)
        """
        # Convert to xyxy if needed
        if xywh:
            px, py, pw, ph = pred_boxes.unbind(-1)
            tx, ty, tw, th = target_boxes.unbind(-1)
            px1 = px - pw / 2
            py1 = py - ph / 2
            px2 = px + pw / 2
            py2 = py + ph / 2
            tx1 = tx - tw / 2
            ty1 = ty - th / 2
            tx2 = tx + tw / 2
            ty2 = ty + th / 2
        else:
            px1, py1, px2, py2 = pred_boxes.unbind(-1)
            tx1, ty1, tx2, ty2 = target_boxes.unbind(-1)
            pw = px2 - px1
            ph = py2 - py1
            tw = tx2 - tx1
            th = ty2 - ty1

        # Intersection
        ix1 = torch.maximum(px1, tx1)
        iy1 = torch.maximum(py1, ty1)
        ix2 = torch.minimum(px2, tx2)
        iy2 = torch.minimum(py2, ty2)
        inter_w = (ix2 - ix1).clamp(0)
        inter_h = (iy2 - iy1).clamp(0)
        inter = inter_w * inter_h

        # Union
        union = pw * ph + tw * th - inter + self.eps

        # Smallest enclosing box
        cx1 = torch.minimum(px1, tx1)
        cy1 = torch.minimum(py1, ty1)
        cx2 = torch.maximum(px2, tx2)
        cy2 = torch.maximum(py2, ty2)
        cw = cx2 - cx1
        ch = cy2 - cy1

        # Center distance
        pcx = (px1 + px2) / 2
        pcy = (py1 + py2) / 2
        tcx = (tx1 + tx2) / 2
        tcy = (ty1 + ty2) / 2

        # Wise-IoU v1 distance attention with detached enclosing dimensions.
        dist = ((pcx - tcx) ** 2 + (pcy - tcy) ** 2) / (cw.square() + ch.square()).detach().clamp_min(self.eps)
        iou_loss = 1 - inter / union
        if self.training and iou_loss.requires_grad and iou_loss.numel():
            with torch.no_grad():
                self.iou_mean.lerp_(iou_loss.detach().mean().to(self.iou_mean), self.momentum)
        # v3 non-monotonic focusing uses an EMA of L_IoU, not an IoU threshold.
        beta = iou_loss.detach() / self.iou_mean.clamp_min(self.eps)
        gain = beta / (self.delta * self.alpha ** (beta - self.delta))
        return gain * torch.exp(dist) * iou_loss


class AlphaIoULoss(nn.Module):
    """
    Alpha-IoU Loss.

    Alpha-IoU generalizes CIoU by introducing a power parameter alpha
    that controls the sensitivity to outliers. When alpha > 1, it provides
    more gradients for well-localized boxes.

    Paper: https://arxiv.org/abs/2110.13675

    Args:
        alpha (float): Power parameter for IoU. Higher values (>1) increase
                       sensitivity to outliers. Default: 2.0
        eps (float): Small value for numerical stability. Default: 1e-7
    """

    def __init__(
        self,
        alpha: float = 2.0,
        eps: float = 1e-7,
    ):
        super().__init__()
        self.alpha = alpha
        self.eps = eps

    def forward(
        self,
        pred_boxes: torch.Tensor,
        target_boxes: torch.Tensor,
        xywh: bool = True,
    ) -> torch.Tensor:
        """
        Calculate Alpha-IoU loss.

        Args:
            pred_boxes (torch.Tensor): Predicted boxes, shape (N, 4)
            target_boxes (torch.Tensor): Target boxes, shape (N, 4)
            xywh (bool): If True, boxes are in (x, y, w, h) format, else (x1, y1, x2, y2)

        Returns:
            torch.Tensor: Alpha-IoU loss values, shape (N,)
        """
        # Convert to xyxy if needed
        if xywh:
            px, py, pw, ph = pred_boxes.unbind(-1)
            tx, ty, tw, th = target_boxes.unbind(-1)
            px1 = px - pw / 2
            py1 = py - ph / 2
            px2 = px + pw / 2
            py2 = py + ph / 2
            tx1 = tx - tw / 2
            ty1 = ty - th / 2
            tx2 = tx + tw / 2
            ty2 = ty + th / 2
        else:
            px1, py1, px2, py2 = pred_boxes.unbind(-1)
            tx1, ty1, tx2, ty2 = target_boxes.unbind(-1)
            pw = px2 - px1
            ph = py2 - py1
            tw = tx2 - tx1
            th = ty2 - ty1

        # Intersection
        ix1 = torch.maximum(px1, tx1)
        iy1 = torch.maximum(py1, ty1)
        ix2 = torch.minimum(px2, tx2)
        iy2 = torch.minimum(py2, ty2)
        inter_w = (ix2 - ix1).clamp(0)
        inter_h = (iy2 - iy1).clamp(0)
        inter = inter_w * inter_h

        # Union
        union = pw * ph + tw * th - inter + self.eps

        # Alpha-IoU
        iou_alpha = (inter / union).clamp(0, 1) ** self.alpha

        # Smallest enclosing box for distance and aspect ratio
        cw = torch.maximum(px2, tx2) - torch.minimum(px1, tx1)
        ch = torch.maximum(py2, ty2) - torch.minimum(py1, ty1)

        # Center distance
        c2 = cw ** 2 + ch ** 2 + self.eps
        rho2 = ((tx1 + tx2 - px1 - px2) ** 2 + (ty1 + ty2 - py1 - py2) ** 2) / 4

        # Aspect ratio consistency (CIoU component with alpha)
        v = (4 / math.pi ** 2) * ((tw / th.clamp_min(self.eps)).atan() - (pw / ph.clamp_min(self.eps)).atan()) ** 2
        with torch.no_grad():
            alpha_v = v / (v - inter / union + (1 + self.eps))

        # Alpha-CIoU
        alphaciou = iou_alpha - (rho2 / c2) ** self.alpha - (v * alpha_v) ** self.alpha
        return alphaciou


# Convenience function to compute all extended IoU losses
def compute_extended_iou(
    box1: torch.Tensor,
    box2: torch.Tensor,
    iou_type: str,
    xywh: bool = True,
    **kwargs
) -> torch.Tensor:
    """
    Compute extended IoU loss.

    Args:
        box1 (torch.Tensor): First set of boxes, shape (N, 4)
        box2 (torch.Tensor): Second set of boxes, shape (N, 4)
        iou_type (str): Type of IoU loss. Options: 'siou', 'eiou', 'wiou', 'alphaiou', 'nwd', 'mpdiou'
        xywh (bool): If True, boxes are in (x, y, w, h) format, else (x1, y1, x2, y2)
        **kwargs: Additional parameters for specific IoU types

    Returns:
        torch.Tensor: IoU values, shape (N,)
    """
    iou_type = iou_type.lower()

    if iou_type == "siou":
        loss_fn = SIoULoss(
            angle_alpha=kwargs.get("siou_angle_alpha", 4.0),
            dist_beta=kwargs.get("siou_dist_beta", 1.0),
            gamma=kwargs.get("siou_gamma", 0.5),
            eps=kwargs.get("eps", 1e-7),
        )
    elif iou_type == "eiou":
        loss_fn = EIoULoss(
            rho=kwargs.get("eiou_rho", 1.0),
            sigma=kwargs.get("eiou_sigma", 1.0),
            eps=kwargs.get("eps", 1e-7),
        )
    elif iou_type == "wiou":
        loss_fn = WIoULoss(
            v_threshold=kwargs.get("wiou_v_threshold", 0.7),
            eps=kwargs.get("wiou_eps", 1e-6),
        )
    elif iou_type in ("alphaiou", "alpha_iou"):
        loss_fn = AlphaIoULoss(
            alpha=kwargs.get("alphaiou_alpha", 2.0),
            eps=kwargs.get("eps", 1e-7),
        )
    elif iou_type == "nwd":
        loss_fn = NWDLoss(
            sigma=kwargs.get("nwd_sigma", 12.8),
            eps=kwargs.get("eps", 1e-7),
        )
    elif iou_type in ("mpdiou", "mpdio"):
        loss_fn = MPDIoULoss(
            eps=kwargs.get("eps", 1e-7),
        )
    else:
        raise ValueError(f"Unknown extended IoU type: {iou_type}")

    if iou_type in ("mpdiou", "mpdio"):
        return loss_fn(box1, box2, xywh=xywh, image_size=kwargs.get("image_size"))
    return loss_fn(box1, box2, xywh=xywh)


class NWDLoss(nn.Module):
    """
    Normalized Wasserstein Distance Loss for small object detection.

    NWD provides a more stable measure for small objects than IoU, as small objects
    are more sensitive to slight position deviations. It computes the Wasserstein distance
    between Gaussian box representations with a fixed pixel-scale constant C.

    Paper: https://arxiv.org/abs/2110.13389

    Args:
        sigma (float): Fixed normalization constant C in pixels. Default: 12.8
        eps (float): Small value for numerical stability. Default: 1e-7
    """

    def __init__(self, sigma: float = 12.8, eps: float = 1e-7):
        super().__init__()
        if sigma <= 0:
            raise ValueError("NWD normalization constant must be positive")
        self.sigma = sigma
        self.eps = eps

    def forward(
        self,
        pred_boxes: torch.Tensor,
        target_boxes: torch.Tensor,
        xywh: bool = True,
    ) -> torch.Tensor:
        """
        Calculate NWD loss.

        Args:
            pred_boxes (torch.Tensor): Predicted boxes, shape (N, 4)
            target_boxes (torch.Tensor): Target boxes, shape (N, 4)
            xywh (bool): If True, boxes are in (x, y, w, h) format, else (x1, y1, x2, y2)

        Returns:
            torch.Tensor: NWD loss values, shape (N,)
        """
        # Convert to xyxy if needed
        if xywh:
            px, py, pw, ph = pred_boxes.unbind(-1)
            tx, ty, tw, th = target_boxes.unbind(-1)
            px1 = px - pw / 2
            py1 = py - ph / 2
            px2 = px + pw / 2
            py2 = py + ph / 2
            tx1 = tx - tw / 2
            ty1 = ty - th / 2
            tx2 = tx + tw / 2
            ty2 = ty + th / 2
        else:
            px1, py1, px2, py2 = pred_boxes.unbind(-1)
            tx1, ty1, tx2, ty2 = target_boxes.unbind(-1)
            pw = px2 - px1
            ph = py2 - py1
            tw = tx2 - tx1
            th = ty2 - ty1

        # Center points
        pcx = (px1 + px2) / 2
        pcy = (py1 + py2) / 2
        tcx = (tx1 + tx2) / 2
        tcy = (ty1 + ty2) / 2

        # Gaussian standard deviations are w/2 and h/2. Vector norm has a
        # finite zero subgradient for perfect boxes, unlike sqrt(0).
        differences = torch.stack((pcx - tcx, pcy - tcy, (pw - tw) / 2, (ph - th) / 2), dim=-1)
        wasserstein_dist = torch.linalg.vector_norm(differences, dim=-1)
        nwd = torch.exp(-wasserstein_dist / self.sigma)

        return 1 - nwd


class MPDIoULoss(nn.Module):
    """
    MPDIoU (Minimum Point Distance IoU) Loss.

    Uses squared distances between corresponding top-left/bottom-right corners,
    normalized by the input image diagonal squared. Boxes must be in pixel units.

    Paper: https://arxiv.org/abs/2307.07662

    Args:
        eps (float): Small value for numerical stability. Default: 1e-7
    """

    def __init__(self, eps: float = 1e-7):
        super().__init__()
        self.eps = eps

    def forward(
        self,
        pred_boxes: torch.Tensor,
        target_boxes: torch.Tensor,
        xywh: bool = True,
        image_size=None,
    ) -> torch.Tensor:
        """
        Calculate MPDIoU loss.

        Args:
            pred_boxes (torch.Tensor): Predicted boxes, shape (N, 4)
            target_boxes (torch.Tensor): Target boxes, shape (N, 4)
            xywh (bool): If True, boxes are in (x, y, w, h) format, else (x1, y1, x2, y2)

        Returns:
            torch.Tensor: MPDIoU loss values, shape (N,)
        """
        # 640**2 exceeds float16's finite range. Promote before any geometry
        # calculation, including direct calls outside the Detect criterion.
        if pred_boxes.dtype in (torch.float16, torch.bfloat16):
            pred_boxes = pred_boxes.float()
        if target_boxes.dtype in (torch.float16, torch.bfloat16):
            target_boxes = target_boxes.float()
        # Convert to xyxy if needed
        if xywh:
            px, py, pw, ph = pred_boxes.unbind(-1)
            tx, ty, tw, th = target_boxes.unbind(-1)
            px1 = px - pw / 2
            py1 = py - ph / 2
            px2 = px + pw / 2
            py2 = py + ph / 2
            tx1 = tx - tw / 2
            ty1 = ty - th / 2
            tx2 = tx + tw / 2
            ty2 = ty + th / 2
        else:
            px1, py1, px2, py2 = pred_boxes.unbind(-1)
            tx1, ty1, tx2, ty2 = target_boxes.unbind(-1)

        d1_sq = (px1 - tx1).square() + (py1 - ty1).square()
        d2_sq = (px2 - tx2).square() + (py2 - ty2).square()
        # Default only serves standalone callers; YOLO passes its actual H,W.
        image_size = (640, 640) if image_size is None else image_size
        dimensions = torch.as_tensor(image_size, device=pred_boxes.device, dtype=pred_boxes.dtype)
        diagonal_sq = dimensions.square().sum().clamp_min(self.eps)

        # Intersection area
        ix1 = torch.maximum(px1, tx1)
        iy1 = torch.maximum(py1, ty1)
        ix2 = torch.minimum(px2, tx2)
        iy2 = torch.minimum(py2, ty2)
        inter_w = (ix2 - ix1).clamp(0)
        inter_h = (iy2 - iy1).clamp(0)
        inter = inter_w * inter_h

        # Union area
        pred_area = (px2 - px1).clamp(0) * (py2 - py1).clamp(0)
        target_area = (tx2 - tx1).clamp(0) * (ty2 - ty1).clamp(0)
        union = pred_area + target_area - inter + self.eps

        # IoU term
        iou = inter / union

        mpdiou = 1 - iou + (d1_sq + d2_sq) / diagonal_sq

        # Losses above one are valid; clamping destroys disjoint-box gradients.
        return mpdiou


def _reduce_mask_loss(loss: torch.Tensor, reduction: str) -> torch.Tensor:
    """Reduce per-instance mask loss values.

    `loss` shape is ``(N,)``, where ``N`` is the number of masks in the first
    dimension after flattening spatial dimensions.  ``reduction="none"`` must
    preserve that vector because ``v8SegmentationLoss`` still needs one value
    per positive mask.
    """
    if reduction == "mean":
        return loss.mean()
    if reduction == "sum":
        return loss.sum()
    if reduction == "none":
        return loss
    raise ValueError(f"Unsupported reduction={reduction!r}. Valid: none, mean, sum")


class DiceLoss(nn.Module):
    """
    Dice Loss for segmentation.

    Dice loss is particularly effective for handling class imbalance in segmentation
    tasks by directly maximizing the overlap between predictions and ground truth.

    Args:
        smooth (float): Smoothing factor to avoid division by zero. Default: 1.0
        reduction (str): Reduction method. Options: 'none', 'mean', 'sum'. Default: 'mean'
    """

    def __init__(self, smooth: float = 1.0, reduction: str = "mean"):
        super().__init__()
        self.smooth = smooth
        self.reduction = reduction

    def forward(self, pred_masks: torch.Tensor, target_masks: torch.Tensor, valid_mask: torch.Tensor | None = None) -> torch.Tensor:
        """
        Calculate Dice loss.

        Args:
            pred_masks (torch.Tensor): Predicted masks (after sigmoid), shape (B, H, W) or (B, N, H, W)
            target_masks (torch.Tensor): Target masks (binary), shape (B, H, W) or (B, N, H, W)
            valid_mask (torch.Tensor | None): Optional mask indicating valid pixels, shape (B, H, W) or (B, N, H, W)

        Returns:
            torch.Tensor: Dice loss value(s), shape depends on reduction
        """
        target_masks = target_masks.float()
        if pred_masks.dim() == 3:
            pred_masks = pred_masks.unsqueeze(1)
        if target_masks.dim() == 3:
            target_masks = target_masks.unsqueeze(1)
        if valid_mask is None:
            valid_mask = torch.ones_like(target_masks)
        elif valid_mask.dim() == 3:
            valid_mask = valid_mask.unsqueeze(1)
        valid_mask = valid_mask.to(dtype=pred_masks.dtype)
        if valid_mask.shape != target_masks.shape:
            valid_mask = valid_mask.expand_as(target_masks)

        pred_flat = pred_masks.reshape(pred_masks.size(0), -1)
        target_flat = target_masks.reshape(target_masks.size(0), -1)
        valid_flat = valid_mask.reshape(valid_mask.size(0), -1)

        intersection = (pred_flat * target_flat * valid_flat).sum(dim=1)
        union = ((pred_flat + target_flat) * valid_flat).sum(dim=1)
        dice = (2.0 * intersection + self.smooth) / (union + self.smooth)
        loss = 1 - dice
        return _reduce_mask_loss(loss, self.reduction)


class FocalTverskyLoss(nn.Module):
    """
    Focal Tversky Loss for segmentation.

    Focal Tversky loss is designed for small object segmentation by controlling
    the trade-off between precision and recall through alpha, beta, and gamma parameters.

    Args:
        alpha (float): Weight of false positives. Default: 0.5
        beta (float): Weight of false negatives. Default: 0.5
        gamma (float): Focal parameter. Default: 1.0
        smooth (float): Smoothing factor. Default: 1e-7
        reduction (str): Reduction method. Options: 'none', 'mean', 'sum'. Default: 'mean'
    """

    def __init__(
        self,
        alpha: float = 0.5,
        beta: float = 0.5,
        gamma: float = 1.0,
        smooth: float = 1e-7,
        reduction: str = "mean",
    ):
        super().__init__()
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma
        self.smooth = smooth
        self.reduction = reduction

    def forward(self, pred_masks: torch.Tensor, target_masks: torch.Tensor, valid_mask: torch.Tensor | None = None) -> torch.Tensor:
        """
        Calculate Focal Tversky loss.

        Args:
            pred_masks (torch.Tensor): Predicted masks (after sigmoid), shape (B, H, W) or (B, N, H, W)
            target_masks (torch.Tensor): Target masks (binary), shape (B, H, W) or (B, N, H, W)
            valid_mask (torch.Tensor | None): Optional mask indicating valid pixels, shape (B, H, W) or (B, N, H, W)

        Returns:
            torch.Tensor: Focal Tversky loss value(s), shape depends on reduction
        """
        target_masks = target_masks.float()
        if pred_masks.dim() == 3:
            pred_masks = pred_masks.unsqueeze(1)
        if target_masks.dim() == 3:
            target_masks = target_masks.unsqueeze(1)
        if valid_mask is None:
            valid_mask = torch.ones_like(target_masks)
        elif valid_mask.dim() == 3:
            valid_mask = valid_mask.unsqueeze(1)
        valid_mask = valid_mask.to(dtype=pred_masks.dtype)
        if valid_mask.shape != target_masks.shape:
            valid_mask = valid_mask.expand_as(target_masks)

        pred_flat = pred_masks.reshape(pred_masks.size(0), -1)
        target_flat = target_masks.reshape(target_masks.size(0), -1)
        valid_flat = valid_mask.reshape(valid_mask.size(0), -1)

        tp = (pred_flat * target_flat * valid_flat).sum(dim=1)
        fp = (pred_flat * (1 - target_flat) * valid_flat).sum(dim=1)
        fn = ((1 - pred_flat) * target_flat * valid_flat).sum(dim=1)
        tversky = (tp + self.smooth) / (tp + self.alpha * fp + self.beta * fn + self.smooth)
        loss = torch.pow(1 - tversky, self.gamma)
        return _reduce_mask_loss(loss, self.reduction)


class ComboLoss(nn.Module):
    """
    Combo Loss for segmentation.

    Combo loss combines BCE and Dice loss to leverage the benefits of both:
    - BCE provides pixel-wise classification guidance
    - Dice loss handles class imbalance and boundary precision

    Args:
        alpha (float): Weight for BCE term. Default: 0.5
        beta (float): Weight for Dice term. Default: 0.5
        smooth (float): Smoothing factor for Dice. Default: 1.0
        reduction (str): Reduction method. Options: 'none', 'mean', 'sum'. Default: 'mean'
    """

    def __init__(
        self,
        alpha: float = 0.5,
        beta: float = 0.5,
        smooth: float = 1.0,
        reduction: str = "mean",
    ):
        super().__init__()
        self.alpha = alpha
        self.beta = beta
        self.smooth = smooth
        self.reduction = reduction

    def forward(self, pred_masks: torch.Tensor, target_masks: torch.Tensor, valid_mask: torch.Tensor | None = None) -> torch.Tensor:
        """
        Calculate Combo loss.

        Args:
            pred_masks (torch.Tensor): Predicted masks (logits before sigmoid), shape (B, H, W) or (B, N, H, W)
            target_masks (torch.Tensor): Target masks (binary), shape (B, H, W) or (B, N, H, W)
            valid_mask (torch.Tensor | None): Optional mask indicating valid pixels, shape (B, H, W) or (B, N, H, W)

        Returns:
            torch.Tensor: Combo loss value(s), shape depends on reduction
        """
        target_masks = target_masks.float()
        if pred_masks.dim() == 3:
            pred_masks = pred_masks.unsqueeze(1)
        if target_masks.dim() == 3:
            target_masks = target_masks.unsqueeze(1)
        if valid_mask is None:
            valid_mask = torch.ones_like(target_masks)
        elif valid_mask.dim() == 3:
            valid_mask = valid_mask.unsqueeze(1)
        valid_mask = valid_mask.to(dtype=pred_masks.dtype)
        if valid_mask.shape != target_masks.shape:
            valid_mask = valid_mask.expand_as(target_masks)

        bce_map = F.binary_cross_entropy_with_logits(pred_masks, target_masks, reduction="none")
        bce_flat = bce_map.reshape(bce_map.size(0), -1)
        valid_flat = valid_mask.reshape(valid_mask.size(0), -1)
        denom = valid_flat.sum(dim=1).clamp_min(1.0)
        bce_loss = (bce_flat * valid_flat).sum(dim=1) / denom

        pred_sigmoid = torch.sigmoid(pred_masks)
        pred_flat = pred_sigmoid.reshape(pred_sigmoid.size(0), -1)
        target_flat = target_masks.reshape(target_masks.size(0), -1)
        intersection = (pred_flat * target_flat * valid_flat).sum(dim=1)
        union = ((pred_flat + target_flat) * valid_flat).sum(dim=1)
        dice = (2.0 * intersection + self.smooth) / (union + self.smooth)
        dice_loss = 1 - dice

        loss = self.alpha * bce_loss + self.beta * dice_loss
        return _reduce_mask_loss(loss, self.reduction)


# Extended function to compute all losses
def compute_mask_loss(
    pred_masks: torch.Tensor,
    target_masks: torch.Tensor,
    loss_type: str,
    **kwargs
) -> torch.Tensor:
    """
    Compute mask/segmentation loss.

    Args:
        pred_masks (torch.Tensor): Predicted masks
        target_masks (torch.Tensor): Target masks
        loss_type (str): Type of loss. Options: 'bce', 'dice', 'focal_tversky', 'combo'
        **kwargs: Additional parameters for specific loss types

    Returns:
        torch.Tensor: Loss value
    """
    loss_type = loss_type.lower()

    if loss_type == "bce":
        return F.binary_cross_entropy_with_logits(pred_masks, target_masks)
    elif loss_type == "dice":
        loss_fn = DiceLoss(
            smooth=kwargs.get("dice_smooth", 1.0),
            reduction=kwargs.get("reduction", "mean"),
        )
        return loss_fn(pred_masks, target_masks)
    elif loss_type == "focal_tversky":
        loss_fn = FocalTverskyLoss(
            alpha=kwargs.get("focal_tversky_alpha", 0.5),
            beta=kwargs.get("focal_tversky_beta", 0.5),
            gamma=kwargs.get("focal_tversky_gamma", 1.0),
            smooth=kwargs.get("focal_tversky_smooth", 1e-7),
            reduction=kwargs.get("reduction", "mean"),
        )
        return loss_fn(pred_masks, target_masks)
    elif loss_type == "combo":
        loss_fn = ComboLoss(
            alpha=kwargs.get("combo_alpha", 0.5),
            beta=kwargs.get("combo_beta", 0.5),
            smooth=kwargs.get("combo_smooth", 1.0),
            reduction=kwargs.get("reduction", "mean"),
        )
        return loss_fn(pred_masks, target_masks)
    else:
        raise ValueError(f"Unknown mask loss type: {loss_type}")
