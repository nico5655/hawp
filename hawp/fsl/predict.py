import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
import os.path as osp
import os

import numpy as np
import torch
from PIL import Image
from hawp.base import to_device, setup_logger, MetricLogger, save_config, show, WireframeGraph

from tqdm import tqdm
from torch.utils.data import DataLoader, IterableDataset, get_worker_info

from hawp.base.utils.logger import setup_logger
from hawp.fsl.config import cfg
from hawp.fsl.dataset.build import build_transform
from hawp.fsl.model.build import build_model


class ImageList(IterableDataset):
    def __init__(self, image_paths, transform):
        super().__init__()
        self.image_paths = image_paths
        self.transform = transform

    def __iter__(self):
        if get_worker_info() is not None:
            raise RuntimeError("Single worker only.")
        for image_path in self.image_paths:
            im = Image.open(image_path)
            w, h = im.size
            meta = {
                "filename": image_path,
                "height": h,
                "width": w,
            }
            im=np.array(im)
            im[im[...,-1]==0]=255
            im=im[...,:-1]
            yield self.transform(im), meta


def parse_args():
    parser = argparse.ArgumentParser(description="HAWP Testing")
    parser.add_argument("config", help="the path of config file")
    parser.add_argument("images", nargs="*")
    parser.add_argument("--ckpt", type=str, required=True)

    parser.add_argument(
        "--j2l", default=None, type=float, help="the threshold for junction-line attraction"
    )
    parser.add_argument("--rscale", default=2, type=int, help="the residual scale")

    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--output", default=None, help="the path of outputs")

    return parser.parse_args()


def set_random_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)


def post_process(output):
    lines_pred = output["lines_pred"]
    juncs_pred = output["juncs_pred"]
    sq_dist1 = ((juncs_pred[:, None] - lines_pred[None, :, :2]) ** 2).sum(dim=-1)
    sq_dist2 = ((juncs_pred[:, None] - lines_pred[None, :, 2:]) ** 2).sum(dim=-1)
    idx1 = sq_dist1.argmin(dim=0)
    idx2 = sq_dist2.argmin(dim=0)
    edges = torch.stack((idx1, idx2)).t()
    [filename] = output["filename"]
    return {
        "edges": edges.cpu().tolist(),
        "edges-weights": output["lines_score"].cpu().tolist(),
        "vertices": juncs_pred.cpu().tolist(),
        "vertices-score": output["juncs_score"].cpu().tolist(),
        "filename": filename,
        "height": output["height"].item(),
        "width": output["width"].item(),
    }


def main():
    args = parse_args()

    config_path = args.config
    cfg.merge_from_file(config_path)

    root = args.output
    if root is None:
        root = str(Path(args.ckpt).parent)

    logger = setup_logger("hawp.predict", root)
    logger.info(args)
    logger.info(f"Loaded configuration file {config_path}")

    show.painters.HAWPainter.confidence_threshold = 0.1
    show.Canvas.show = False
    painter = show.painters.HAWPainter()
    set_random_seed(args.seed)

    device = cfg.MODEL.DEVICE
    logger.info(f"Running on device {device}")
    model = build_model(cfg).to(device)

    if args.rscale is not None:
        model.use_residual = args.rscale

    if args.j2l:
        model.j2l_threshold = args.j2l

    ckpt = torch.load(args.ckpt, map_location="cpu")
    model.load_state_dict(ckpt["model"])
    model = model.eval()

    transform = build_transform(cfg)

    dataset = ImageList(args.images, transform=transform)
    dataloader = DataLoader(dataset, batch_size=1, pin_memory=True)

    outputs = []
    timings = defaultdict(float)
    for tensor, meta in tqdm(dataloader, total=len(args.images)):
        fname=meta['filename'][0]
        pname=Path(fname)
        try:
            with torch.no_grad():
                output, extra_info = model(tensor.to(device), [meta])
        except Exception as e:
            print(e)
            print('skip')
            continue
        outputs.append(post_process(output))
        for key, value in extra_info.items():
            timings[key] += value

        fig_file = osp.join(args.output,pname.with_suffix('.png').name)

        if output['lines_pred'] is None:
          print('skip')
          continue
        with show.image_canvas(fname, fig_file=fig_file) as ax:
            painter.draw_wireframe(ax,output)

        # if args.saveto and args.
        indices = WireframeGraph.xyxy2indices(output['juncs_pred'],output['lines_pred'])

        wireframe = WireframeGraph(output['juncs_pred'], output['juncs_score'], indices, output['lines_score'], output['width'], output['height'])

    logger.info(f"Timings : {dict(timings)}")

    out_path = Path(root) / "hawp.json"
    logger.info(f"Writing outputs to {out_path}")
    with out_path.open("w") as f:
        json.dump(outputs, f)


if __name__ == "__main__":
    main()
