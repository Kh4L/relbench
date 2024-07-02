import argparse
import copy
import os
from typing import Dict, Tuple

import numpy as np
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn.functional as F
from inferred_stypes import dataset2inferred_stypes
from model import Model
from text_embedder import GloveTextEmbedding
from torch import Tensor
from torch.nn.parallel import DistributedDataParallel
from torch_frame.config.text_embedder import TextEmbedderConfig
from torch_geometric.loader import NeighborLoader
from torch_geometric.seed import seed_everything
from tqdm import tqdm

from relbench.data import LinkTask, RelBenchDataset
from relbench.data.task_base import TaskType
from relbench.datasets import get_dataset
from relbench.external.graph import get_link_train_table_input, make_pkey_fkey_graph
from relbench.external.loader import LinkNeighborLoader

tune_metric = "link_prediction_map"


def run_main(rank, world_size, args, data, task, col_stats_dict):
    if not torch.cuda.is_available():
        device = torch.device("cpu")
    else:
        device = torch.device("cuda:" + str(rank))
        if world_size > 1:
            # init pytorch worker
            os.environ["MASTER_ADDR"] = "localhost"
            os.environ["MASTER_PORT"] = "12355"
            dist.init_process_group("nccl", rank=rank, world_size=world_size)

    num_neighbors = [int(args.num_neighbors // 2**i) for i in range(args.num_layers)]

    train_table_input = get_link_train_table_input(task.train_table, task)

    # src_nodes = (train_table_input.src_nodes[0],
    #             train_table_input.src_nodes[1].split(train_table_input.src_nodes[1].shape[0] // world_size)[rank])

    # dst_nodes = (train_table_input.dst_nodes[0],
    #             train_table_input.dst_nodes[1].split(train_table_input.dst_nodes[1].shape[0] // world_size)[rank])
    #
    split_size = (
        train_table_input.dst_nodes[1].shape[0] + world_size - 1
    ) // world_size
    start_idx = rank * split_size
    end_idx = min(start_idx + split_size, train_table_input.dst_nodes[1].shape[0])
    indices = (
        torch.arange(start_idx, end_idx)
        .long()
        .to(train_table_input.dst_nodes[1].device)
    )
    dst_nodes_tensor = torch.index_select(
        train_table_input.dst_nodes[1].to_sparse_coo(), 0, indices
    )
    dst_nodes = (train_table_input.dst_nodes[0], dst_nodes_tensor.to_sparse_csr())

    src_nodes = (
        train_table_input.src_nodes[0],
        train_table_input.src_nodes[1][indices],
    )
    src_time = train_table_input.src_time[indices]

    train_loader = LinkNeighborLoader(
        data=data,
        num_neighbors=num_neighbors,
        time_attr="time",
        src_nodes=src_nodes,
        dst_nodes=dst_nodes,
        num_dst_nodes=train_table_input.num_dst_nodes,
        src_time=src_time,
        share_same_time=args.share_same_time,
        batch_size=args.batch_size,
        temporal_strategy=args.temporal_strategy,
        # if share_same_time is True, we use sampler, so shuffle must be set False
        shuffle=not args.share_same_time,
        num_workers=args.num_workers,
    )

    # first eval/test run only rank 0
    if rank == 0:
        eval_loaders_dict: Dict[str, Tuple[NeighborLoader, NeighborLoader]] = {}
        for split in ["val", "test"]:
            seed_time = task.val_seed_time if split == "val" else task.test_seed_time
            target_table = task.val_table if split == "val" else task.test_table
            src_node_indices = torch.from_numpy(
                target_table.df[task.src_entity_col].values
            )
            src_loader = NeighborLoader(
                data,
                num_neighbors=num_neighbors,
                time_attr="time",
                input_nodes=(task.src_entity_table, src_node_indices),
                input_time=torch.full(
                    size=(len(src_node_indices),),
                    fill_value=seed_time,
                    dtype=torch.long,
                ),
                batch_size=args.batch_size,
                shuffle=False,
                num_workers=args.num_workers,
            )
            dst_loader = NeighborLoader(
                data,
                num_neighbors=num_neighbors,
                time_attr="time",
                input_nodes=task.dst_entity_table,
                input_time=torch.full(
                    size=(task.num_dst_nodes,), fill_value=seed_time, dtype=torch.long
                ),
                batch_size=args.batch_size,
                shuffle=False,
                num_workers=args.num_workers,
            )
            eval_loaders_dict[split] = (src_loader, dst_loader)

    model = Model(
        data=data,
        col_stats_dict=col_stats_dict,
        num_layers=args.num_layers,
        channels=args.channels,
        out_channels=args.channels,
        aggr=args.aggr,
        norm="layer_norm",
        shallow_list=[task.dst_entity_table] if args.use_shallow else [],
    ).to(device)

    if world_size > 1:
        model = DistributedDataParallel(model, device_ids=[rank], static_graph=True)

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    def train() -> float:
        model.train()

        loss_accum = count_accum = 0
        steps = 0
        total_steps = min(len(train_loader) * world_size, args.max_steps_per_epoch)
        if rank == 0:
            progress_bar = tqdm(desc="Training", total=total_steps)

        for batch in train_loader:
            src_batch, batch_pos_dst, batch_neg_dst = batch
            src_batch, batch_pos_dst, batch_neg_dst = (
                src_batch.to(device),
                batch_pos_dst.to(device),
                batch_neg_dst.to(device),
            )
            x_src = model(src_batch, task.src_entity_table)
            x_pos_dst = model(batch_pos_dst, task.dst_entity_table)
            x_neg_dst = model(batch_neg_dst, task.dst_entity_table)

            # [batch_size, ]
            pos_score = torch.sum(x_src * x_pos_dst, dim=1)
            if args.share_same_time:
                # [batch_size, batch_size]
                neg_score = x_src @ x_neg_dst.t()
                # [batch_size, 1]
                pos_score = pos_score.view(-1, 1)
            else:
                # [batch_size, ]
                neg_score = torch.sum(x_src * x_neg_dst, dim=1)
            optimizer.zero_grad()
            # BPR loss
            diff_score = pos_score - neg_score
            loss = F.softplus(-diff_score).mean()
            loss.backward()
            optimizer.step()

            loss_accum += float(loss) * x_src.size(0)
            count_accum += x_src.size(0)

            steps += 1
            if rank == 0:
                progress_bar.update(world_size)
            if steps > args.max_steps_per_epoch:
                break

        return loss_accum / count_accum

    @torch.no_grad()
    def test(src_loader: NeighborLoader, dst_loader: NeighborLoader) -> np.ndarray:
        model.eval()

        dst_embs: list[Tensor] = []
        for batch in tqdm(dst_loader):
            batch = batch.to(device)
            emb = model(batch, task.dst_entity_table).detach()
            dst_embs.append(emb)
        dst_emb = torch.cat(dst_embs, dim=0)
        del dst_embs

        pred_index_mat_list: list[Tensor] = []
        for batch in tqdm(src_loader):
            batch = batch.to(device)
            emb = model(batch, task.src_entity_table)
            _, pred_index_mat = torch.topk(emb @ dst_emb.t(), k=task.eval_k, dim=1)
            pred_index_mat_list.append(pred_index_mat.cpu())
        pred = torch.cat(pred_index_mat_list, dim=0).numpy()
        return pred

    state_dict = None
    best_val_metric = 0
    for epoch in range(1, args.epochs + 1):
        train_loss = train()
        if rank == 0:
            if epoch % args.eval_epochs_interval == 0:
                val_pred = test(*eval_loaders_dict["val"])
                val_metrics = task.evaluate(val_pred, task.val_table)
                print(
                    f"Epoch: {epoch:02d}, Train loss: {train_loss}, "
                    f"Val metrics: {val_metrics}"
                )

                if val_metrics[tune_metric] >= best_val_metric:
                    best_val_metric = val_metrics[tune_metric]
                    state_dict = copy.deepcopy(model.state_dict())

    if rank == 0:
        model.load_state_dict(state_dict)
        val_pred = test(*eval_loaders_dict["val"])
        val_metrics = task.evaluate(val_pred, task.val_table)
        print(f"Best Val metrics: {val_metrics}")

        test_pred = test(*eval_loaders_dict["test"])
        test_metrics = task.evaluate(test_pred)
        print(f"Best test metrics: {test_metrics}")


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, default="rel-hm")
    parser.add_argument("--task", type=str, default="user-item-purchase")
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--eval_epochs_interval", type=int, default=1)
    parser.add_argument("--batch_size", type=int, default=512)
    parser.add_argument("--channels", type=int, default=128)
    parser.add_argument("--aggr", type=str, default="sum")
    parser.add_argument("--num_layers", type=int, default=2)
    parser.add_argument("--num_neighbors", type=int, default=160)
    parser.add_argument("--temporal_strategy", type=str, default="uniform")
    # Use the same seed time across the mini-batch and share the negatives
    parser.add_argument("--share_same_time", action="store_true", default=True)
    # Whether to use shallow embedding on dst nodes or not.
    parser.add_argument("--use_shallow", action="store_true", default=True)
    parser.add_argument("--max_steps_per_epoch", type=int, default=2000)
    parser.add_argument("--num_workers", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--cache_dir",
        type=str,
        default=os.path.expanduser("~/.cache/relbench/materialized"),
    )
    args = parser.parse_args()

    preproc_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if torch.cuda.is_available():
        torch.set_num_threads(1)

    seed_everything(args.seed)

    dataset: RelBenchDataset = get_dataset(name=args.dataset, process=False)
    task: LinkTask = dataset.get_task(args.task, process=True)
    assert task.task_type == TaskType.LINK_PREDICTION

    col_to_stype_dict = dataset2inferred_stypes[args.dataset]

    data, col_stats_dict = make_pkey_fkey_graph(
        dataset.db,
        col_to_stype_dict=col_to_stype_dict,
        text_embedder_cfg=TextEmbedderConfig(
            text_embedder=GloveTextEmbedding(device=preproc_device), batch_size=256
        ),
        cache_dir=os.path.join(args.cache_dir, args.dataset),
    )

    world_size = max(torch.cuda.device_count(), 1)
    if torch.cuda.is_available():
        print("Let's use", world_size, "GPUs!")
    if world_size > 1:
        mp.spawn(
            run_main,
            args=(world_size, args, data, task, col_stats_dict),
            nprocs=world_size,
            join=True,
        )
    else:
        run_main(0, 1, args, data, task, col_stats_dict)