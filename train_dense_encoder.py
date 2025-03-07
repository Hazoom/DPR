#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.


"""
 Pipeline to train DPR Biencoder
"""
import collections
import logging
import math
import os
import random
import sys
import time
from datetime import datetime
from typing import Tuple, Dict, List

import hydra
import numpy as np
import torch
import wandb
from omegaconf import DictConfig, OmegaConf
from torch import Tensor as T
from torch import nn
import pandas as pd

from dpr.data.biencoder_data import get_dpr_files, BiEncoderPassage, BiEncoderSample
from dpr.metrics.data_classes import IRMetrics
from dpr.metrics.retriever_metrics_utils import calculate_ir_scores
from dpr.models import init_biencoder_components
from dpr.models.biencoder import BiEncoder, BiEncoderNllLoss, BiEncoderBatch
from dpr.options import (
    setup_cfg_gpu,
    set_seed,
    get_encoder_params_state_from_cfg,
    set_cfg_params_from_state,
    setup_logger,
)
from dpr.utils.conf_utils import BiencoderDatasetsCfg
from dpr.utils.data_utils import (
    ShardedDataIterator,
    Tensorizer,
    MultiSetDataIterator,
    read_data_from_json_files,
)
from dpr.utils.dist_utils import all_gather_list
from dpr.utils.model_utils import (
    setup_for_distributed_mode,
    move_to_device,
    get_schedule_linear,
    CheckpointState,
    get_model_file,
    get_model_obj,
    load_states_from_checkpoint,
)
from generate_dense_embeddings import gen_ctx_vectors

logger = logging.getLogger()
setup_logger(logger)


def flatten_dict(d, parent_key='', sep='_'):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


class BiEncoderTrainer(object):
    """
    BiEncoder training pipeline component. Can be used to initiate or resume training and validate the trained model
    using either binary classification's NLL loss or average rank of the question's gold passages across dataset
    provided pools of negative passages. For full IR accuracy evaluation, please see generate_dense_embeddings.py
    and dense_retriever.py CLI tools.
    """

    def __init__(self, cfg: DictConfig):
        self.shard_id = cfg.local_rank if cfg.local_rank != -1 else 0
        self.distributed_factor = cfg.distributed_world_size or 1

        logger.info("***** Initializing components for training *****")

        # if model file is specified, encoder parameters from saved state should be used for initialization
        model_file = get_model_file(cfg, cfg.checkpoint_file_name)
        saved_state = None
        if model_file:
            saved_state = load_states_from_checkpoint(model_file)
            set_cfg_params_from_state(saved_state.encoder_params, cfg)

        tensorizer, model, optimizer = init_biencoder_components(cfg.encoder.encoder_model_type, cfg)

        model, optimizer = setup_for_distributed_mode(
            model,
            optimizer,
            cfg.device,
            cfg.n_gpu,
            cfg.local_rank,
            cfg.fp16,
            cfg.fp16_opt_level,
        )
        wandb.watch(model)

        self.biencoder = model
        self.optimizer = optimizer
        self.tensorizer = tensorizer
        self.start_epoch = 0
        self.start_batch = 0
        self.scheduler_state = None
        self.best_validation_result = None
        self.best_cp_name = None
        self.cfg = cfg
        self.ds_cfg = BiencoderDatasetsCfg(cfg)

        if saved_state:
            self._load_saved_state(saved_state)

        self.dev_iterator = None

    def get_data_iterator(
        self,
        batch_size: int,
        is_train_set: bool,
        shuffle=True,
        shuffle_seed: int = 0,
        offset: int = 0,
        rank: int = 0,
    ):

        hydra_datasets = self.ds_cfg.train_datasets if is_train_set else self.ds_cfg.dev_datasets
        sampling_rates = self.ds_cfg.sampling_rates

        logger.info(
            "Initializing task/set data %s",
            self.ds_cfg.train_datasets_names if is_train_set else self.ds_cfg.dev_datasets_names,
        )

        # randomized data loading to avoid file system congestion
        datasets_list = [ds for ds in hydra_datasets]
        rnd = random.Random(rank)
        rnd.shuffle(datasets_list)
        [ds.load_data() for ds in datasets_list]

        sharded_iterators = [
            ShardedDataIterator(
                ds,
                shard_id=self.shard_id,
                num_shards=self.distributed_factor,
                batch_size=batch_size,
                shuffle=shuffle,
                shuffle_seed=shuffle_seed,
                offset=offset,
            )
            for ds in hydra_datasets
        ]

        return MultiSetDataIterator(
            sharded_iterators,
            shuffle_seed,
            shuffle,
            sampling_rates=sampling_rates if is_train_set else [1],
            rank=rank,
        )

    def run_train(self):
        cfg = self.cfg

        all_passages: List[Tuple[object, BiEncoderPassage]] = []
        if cfg.customer_chunks:
            logger.info(f"Reading customer chunks from {cfg.customer_chunks}")
            customer_chunks_file_paths = get_dpr_files(cfg.customer_chunks)
            if not customer_chunks_file_paths:
                raise ValueError(f"File descriptor {cfg.customer_chunks} is not configured.")
            customer_chunks = read_data_from_json_files(customer_chunks_file_paths)

            logger.info("Converting customer chunks to BiEncoderPassage objects")
            if customer_chunks:
                for customer_name, chunks in customer_chunks.items():
                    all_passages.extend([
                        (chunk["chunk_index"], BiEncoderPassage(
                            text=chunk["chunk"],
                            title=chunk["title"],
                            url=chunk["url"],
                            chunk_index=chunk["chunk_index"],
                            chunk_meta=chunk["meta"],
                            customer_name=customer_name
                        )) for chunk in chunks.values()
                    ])

        train_iterator = self.get_data_iterator(
            cfg.train.batch_size,
            True,
            shuffle=True,
            shuffle_seed=cfg.seed,
            offset=self.start_batch,
            rank=cfg.local_rank,
        )
        max_iterations = train_iterator.get_max_iterations()
        logger.info("  Total iterations per epoch=%d", max_iterations)
        if max_iterations == 0:
            logger.warning("No data found for training.")
            return

        updates_per_epoch = train_iterator.max_iterations // cfg.train.gradient_accumulation_steps

        total_updates = updates_per_epoch * cfg.train.num_train_epochs
        logger.info(" Total updates=%d", total_updates)
        warmup_steps = cfg.train.warmup_steps

        if self.scheduler_state:
            # TODO: ideally we'd want to just call
            # scheduler.load_state_dict(self.scheduler_state)
            # but it doesn't work properly as of now

            logger.info("Loading scheduler state %s", self.scheduler_state)
            shift = int(self.scheduler_state["last_epoch"])
            logger.info("Steps shift %d", shift)
            scheduler = get_schedule_linear(
                self.optimizer,
                warmup_steps,
                total_updates,
                steps_shift=shift,
            )
        else:
            scheduler = get_schedule_linear(self.optimizer, warmup_steps, total_updates)

        eval_step = math.ceil(updates_per_epoch / cfg.train.eval_per_epoch)
        logger.info("  Eval step = %d", eval_step)
        logger.info("***** Training *****")

        for epoch in range(self.start_epoch, int(cfg.train.num_train_epochs)):
            logger.info("***** Epoch %d *****", epoch)
            epoch_metrics: Dict[str, float] = self._train_epoch(
                scheduler, epoch, eval_step, train_iterator, all_passages
            )
            wandb.log(epoch_metrics)

        if cfg.local_rank in [-1, 0]:
            logger.info("Training finished. Best validation checkpoint %s", self.best_cp_name)

    def validate_and_save(
        self,
        epoch: int,
        iteration: int,
        scheduler,
        all_passages: List[Tuple[object, BiEncoderPassage]],
        save: bool = True,
    ) -> Dict[str, float]:
        metrics: Dict[str, float] = {}

        cfg = self.cfg
        # for distributed mode, save checkpoint for only one process
        save_cp = cfg.local_rank in [-1, 0]

        if epoch == cfg.val_av_rank_start_epoch:
            self.best_validation_result = None

        if not cfg.dev_datasets:
            validation_loss = 0
        else:
            metrics = self.validate_nll()
            p_at_30 = None
            if all_passages:
                ask_ai_ir_metrics = self.validate_ask_ai_metrics(all_passages)
                metrics.update(ask_ai_ir_metrics)
                p_at_30 = ask_ai_ir_metrics["p@30"]
            average_rank_loss = self.validate_average_rank()
            metrics["Dev Average Rank"] = average_rank_loss

            if epoch >= cfg.val_av_rank_start_epoch:
                if p_at_30 is not None:
                    validation_loss = p_at_30
                else:
                    validation_loss = average_rank_loss
            else:
                validation_loss = metrics["Dev NLL loss"]

        if save_cp and save:
            best_model_found = False
            cp_name = None

            if (not cfg.train.higher_is_better and (
                    validation_loss < (self.best_validation_result or validation_loss + 1))) or (
                    cfg.train.higher_is_better and (
                    validation_loss > (self.best_validation_result or validation_loss - 1))
            ):
                best_model_found = True

            if not cfg.train.save_best_only or (cfg.train.save_best_only and best_model_found):
                cp_name = self._save_checkpoint(
                    scheduler,
                    epoch,
                    iteration,
                    save_separate_models=cfg.train.save_separate_models,
                    best_model_found=cfg.train.save_best_only and best_model_found
                )

            if best_model_found and cp_name:
                self.best_validation_result = validation_loss
                self.best_cp_name = cp_name
                logger.info("New Best validation checkpoint %s", cp_name)

        return metrics

    def validate_nll(self) -> Dict[str, float]:
        logger.info("NLL validation ...")

        metrics: Dict[str, float] = {}

        cfg = self.cfg
        self.biencoder.eval()

        if not self.dev_iterator:
            self.dev_iterator = self.get_data_iterator(
                cfg.train.dev_batch_size, False, shuffle=False, rank=cfg.local_rank
            )
        data_iterator = self.dev_iterator

        total_loss = 0.0
        total_correct_predictions = 0
        num_hard_negatives = cfg.train.hard_negatives
        num_other_negatives = cfg.train.other_negatives
        batches = 0
        dataset = 0

        for i, samples_batch in enumerate(data_iterator.iterate_ds_data()):
            if isinstance(samples_batch, Tuple):
                samples_batch, dataset = samples_batch

            biencoder_input = BiEncoder.create_biencoder_input2(
                samples_batch,
                self.tensorizer,
                True,
                num_hard_negatives,
                num_other_negatives,
                shuffle=False,
            )

            # get the token to be used for representation selection
            ds_cfg = self.ds_cfg.dev_datasets[dataset]
            rep_positions = ds_cfg.selector.get_positions(biencoder_input.question_ids, self.tensorizer)
            encoder_type = ds_cfg.encoder_type

            loss, correct_cnt = _do_biencoder_fwd_pass(
                self.biencoder,
                biencoder_input,
                self.tensorizer,
                cfg,
                encoder_type=encoder_type,
                rep_positions=rep_positions,
            )
            total_loss += loss.item()
            total_correct_predictions += correct_cnt
            batches += 1

        total_loss = total_loss / batches
        total_samples = batches * cfg.train.dev_batch_size * self.distributed_factor
        correct_ratio = float(total_correct_predictions / total_samples)
        logger.info(
            "NLL Validation: loss = %f. correct prediction ratio  %d/%d ~  %f",
            total_loss,
            total_correct_predictions,
            total_samples,
            correct_ratio,
        )

        metrics["Dev Correct Predictions Ratio"] = correct_ratio
        metrics["Dev Total Correct Predictions"] = total_correct_predictions
        metrics["Dev Total Samples"] = total_samples
        metrics["Dev NLL loss"] = total_loss

        return metrics

    def validate_ask_ai_metrics(self, all_passages: List[Tuple[object, BiEncoderPassage]]) -> Dict:
        logger.info("AskAI IR metrics")

        passage_id_to_passage = {passage_id: passage for passage_id, passage in all_passages}

        encoded_passages_and_ids: List[Tuple[object, np.array]] = self._encode_all_passages(all_passages)
        passage_index_to_passage_id = {
            index: passage_and_id[0] for index, passage_and_id in enumerate(encoded_passages_and_ids)
        }
        encoded_passages = torch.tensor([passage[1] for passage in encoded_passages_and_ids])

        q_represenations, _, positive_idx_per_question, all_samples = self._encode_questions_and_passages(
            without_negatives=True, return_samples=True,
        )

        logger.info("AskAI IR validation: total q_vectors size=%s", q_represenations.size())
        logger.info("AskAI IR validation: total ctx_vectors size=%s", encoded_passages.size())

        # compute similarity score between all questions to all passages
        sim_score_f = BiEncoderNllLoss.get_similarity_function()
        scores = sim_score_f(q_represenations, encoded_passages)
        values, indices = torch.sort(scores, dim=1, descending=True)

        indices = indices.tolist()

        ir_metrics: List[Dict] = []
        for index, sample in enumerate(all_samples):
            # for each question, get top k passages using their indices
            retrieved_passage_indices = indices[index][0:self.cfg.train.top_k]
            retrieved_passages_ids = [
                passage_index_to_passage_id[passage_index] for passage_index in retrieved_passage_indices
            ]
            retrieved_passages = [passage_id_to_passage[passage_id] for passage_id in retrieved_passages_ids]
            sample_ir_metrics: IRMetrics = calculate_ir_scores(sample.positive_passages, retrieved_passages)

            if_metrics_dict = {
                "p@1": sample_ir_metrics.rank_to_p_metrics[1].precision,
                "p@2": sample_ir_metrics.rank_to_p_metrics[2].precision,
                "p@5": sample_ir_metrics.rank_to_p_metrics[5].precision,
                "p@30": sample_ir_metrics.rank_to_p_metrics[30].precision,
                "url_rank": sample_ir_metrics.article_hit_scores_rank,
                "section_rank": sample_ir_metrics.section_hit_scores_rank,
                "p1_Url": sample_ir_metrics.rank_to_p_metrics[1].url,
                "p2_Url": sample_ir_metrics.rank_to_p_metrics[2].url,
                "p5_Url": sample_ir_metrics.rank_to_p_metrics[5].url,
                "p30_Url": sample_ir_metrics.rank_to_p_metrics[30].url,
                "p1_Section": sample_ir_metrics.rank_to_p_metrics[1].section,
                "p2_Section": sample_ir_metrics.rank_to_p_metrics[2].section,
                "p5_Section": sample_ir_metrics.rank_to_p_metrics[5].section,
                "p30_Section": sample_ir_metrics.rank_to_p_metrics[30].section,
            }

            ir_metrics.append(if_metrics_dict)

        all_preds_df = pd.DataFrame(ir_metrics)
        preds_df = (all_preds_df.mean() * 100).round(1)

        metrics = preds_df.to_dict()

        logger.info("IR metrics: %s", str(metrics))

        return metrics

    def validate_average_rank(self) -> float:
        """
        Validates biencoder model using each question's gold passage's rank across the set of passages from the dataset.
        It generates vectors for specified amount of negative passages from each question (see --val_av_rank_xxx params)
        and stores them in RAM as well as question vectors.
        Then the similarity scores are calculted for the entire
        num_questions x (num_questions x num_passages_per_question) matrix and sorted per quesrtion.
        Each question's gold passage rank in that  sorted list of scores is averaged across all the questions.
        :return: averaged rank number
        """
        logger.info("Average rank validation ...")

        self.biencoder.eval()

        q_represenations, ctx_represenations, positive_idx_per_question, _ = self._encode_questions_and_passages()

        logger.info("Av.rank validation: total q_vectors size=%s", q_represenations.size())
        logger.info("Av.rank validation: total ctx_vectors size=%s", ctx_represenations.size())

        q_num = q_represenations.size(0)
        assert q_num == len(positive_idx_per_question)

        sim_score_f = BiEncoderNllLoss.get_similarity_function()

        scores = sim_score_f(q_represenations, ctx_represenations)
        values, indices = torch.sort(scores, dim=1, descending=True)

        rank = 0
        for i, idx in enumerate(positive_idx_per_question):
            # aggregate the rank of the known gold passage in the sorted results for each question
            gold_idx = (indices[i] == idx).nonzero()
            rank += gold_idx.item()

        if self.distributed_factor > 1:
            # each node calcuated its own rank, exchange the information between node and calculate the "global" average rank
            # NOTE: the set of passages is still unique for every node
            eval_stats = all_gather_list([rank, q_num], max_size=100)
            for i, item in enumerate(eval_stats):
                remote_rank, remote_q_num = item
                if i != self.cfg.local_rank:
                    rank += remote_rank
                    q_num += remote_q_num

        av_rank = float(rank / q_num)
        logger.info("Av.rank validation: average rank %s, total questions=%d", av_rank, q_num)

        return av_rank

    def _encode_all_passages(self, all_passages: List[Tuple[object, BiEncoderPassage]]) -> List[Tuple[object, np.array]]:
        encoded_passages: List[Tuple[object, np.array]] = gen_ctx_vectors(
            self.cfg.train.val_av_rank_bsz, self.cfg.device, all_passages, self.biencoder.ctx_model, self.tensorizer
        )
        return encoded_passages

    def _encode_questions_and_passages(self, without_negatives: bool = False, return_samples: bool = False):
        q_represenations = []
        ctx_represenations = []
        positive_idx_per_question = []
        all_samples: List[BiEncoderSample] = []

        cfg = self.cfg

        if not self.dev_iterator:
            self.dev_iterator = self.get_data_iterator(
                cfg.train.dev_batch_size, False, shuffle=False, rank=cfg.local_rank
            )

        data_iterator = self.dev_iterator

        sub_batch_size = cfg.train.val_av_rank_bsz

        num_hard_negatives = cfg.train.val_av_rank_hard_neg
        num_other_negatives = cfg.train.val_av_rank_other_neg

        if without_negatives:
            num_hard_negatives = 0
            num_other_negatives = 0

        dataset = 0
        for i, samples_batch in enumerate(data_iterator.iterate_ds_data()):
            # samples += 1
            if len(q_represenations) > cfg.train.val_av_rank_max_qs / self.distributed_factor:
                break

            if isinstance(samples_batch, Tuple):
                samples_batch, dataset = samples_batch

            if return_samples:
                all_samples.extend(samples_batch)

            biencoder_input = BiEncoder.create_biencoder_input2(
                samples_batch,
                self.tensorizer,
                True,
                num_hard_negatives,
                num_other_negatives,
                shuffle=False,
            )

            biencoder_input = BiEncoderBatch(**move_to_device(biencoder_input._asdict(), cfg.device))

            total_ctxs = len(ctx_represenations)
            ctxs_ids = biencoder_input.context_ids
            ctxs_segments = biencoder_input.ctx_segments
            bsz = ctxs_ids.size(0)

            # get the token to be used for representation selection
            ds_cfg = self.ds_cfg.dev_datasets[dataset]
            encoder_type = ds_cfg.encoder_type
            rep_positions = ds_cfg.selector.get_positions(biencoder_input.question_ids, self.tensorizer)

            # split contexts batch into sub batches since it is supposed to be too large to be processed in one batch
            for j, batch_start in enumerate(range(0, bsz, sub_batch_size)):

                q_ids, q_segments = (
                    (biencoder_input.question_ids, biencoder_input.question_segments) if j == 0 else (None, None)
                )

                if j == 0 and cfg.n_gpu > 1 and q_ids.size(0) == 1:
                    # if we are in DP (but not in DDP) mode, all model input tensors should have batch size >1 or 0,
                    # otherwise the other input tensors will be split but only the first split will be called
                    continue

                ctx_ids_batch = ctxs_ids[batch_start: batch_start + sub_batch_size]
                ctx_seg_batch = ctxs_segments[batch_start: batch_start + sub_batch_size]

                q_attn_mask = self.tensorizer.get_attn_mask(q_ids)
                ctx_attn_mask = self.tensorizer.get_attn_mask(ctx_ids_batch)
                with torch.no_grad():
                    q_dense, ctx_dense = self.biencoder(
                        q_ids,
                        q_segments,
                        q_attn_mask,
                        ctx_ids_batch,
                        ctx_seg_batch,
                        ctx_attn_mask,
                        encoder_type=encoder_type,
                        representation_token_pos=rep_positions,
                    )

                if q_dense is not None:
                    q_represenations.extend(q_dense.cpu().split(1, dim=0))

                ctx_represenations.extend(ctx_dense.cpu().split(1, dim=0))

            batch_positive_idxs = biencoder_input.is_positive
            positive_idx_per_question.extend([total_ctxs + v for v in batch_positive_idxs])

        ctx_represenations = torch.cat(ctx_represenations, dim=0)
        q_represenations = torch.cat(q_represenations, dim=0)

        return q_represenations, ctx_represenations, positive_idx_per_question, all_samples

    def _train_epoch(
        self,
        scheduler,
        epoch: int,
        eval_step: int,
        train_data_iterator: MultiSetDataIterator,
        all_passages: List[Tuple[object, BiEncoderPassage]],
    ) -> Dict[str, float]:

        cfg = self.cfg
        rolling_train_loss = 0.0
        epoch_loss = 0
        epoch_correct_predictions = 0

        log_result_step = cfg.train.log_batch_step
        rolling_loss_step = cfg.train.train_rolling_loss_step
        num_hard_negatives = cfg.train.hard_negatives
        num_other_negatives = cfg.train.other_negatives
        seed = cfg.seed
        self.biencoder.train()
        epoch_batches = train_data_iterator.max_iterations
        data_iteration = 0

        metrics: Dict[str, float] = {}

        dataset = 0
        for i, samples_batch in enumerate(train_data_iterator.iterate_ds_data(epoch=epoch)):
            if isinstance(samples_batch, Tuple):
                samples_batch, dataset = samples_batch

            ds_cfg = self.ds_cfg.train_datasets[dataset]
            special_token = ds_cfg.special_token
            encoder_type = ds_cfg.encoder_type
            shuffle_positives = ds_cfg.shuffle_positives

            # to be able to resume shuffled ctx- pools
            data_iteration = train_data_iterator.get_iteration()
            random.seed(seed + epoch + data_iteration)

            biencoder_batch = BiEncoder.create_biencoder_input2(
                samples_batch,
                self.tensorizer,
                True,
                num_hard_negatives,
                num_other_negatives,
                shuffle=True,
                shuffle_positives=shuffle_positives,
                query_token=special_token,
            )

            # get the token to be used for representation selection
            from dpr.data.biencoder_data import DEFAULT_SELECTOR

            selector = ds_cfg.selector if ds_cfg else DEFAULT_SELECTOR

            rep_positions = selector.get_positions(biencoder_batch.question_ids, self.tensorizer)

            loss_scale = cfg.loss_scale_factors[dataset] if cfg.loss_scale_factors else None
            loss, correct_cnt = _do_biencoder_fwd_pass(
                self.biencoder,
                biencoder_batch,
                self.tensorizer,
                cfg,
                encoder_type=encoder_type,
                rep_positions=rep_positions,
                loss_scale=loss_scale,
            )

            epoch_correct_predictions += correct_cnt
            epoch_loss += loss.item()
            rolling_train_loss += loss.item()

            if cfg.fp16:
                from apex import amp

                with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                    scaled_loss.backward()
                if cfg.train.max_grad_norm > 0:
                    torch.nn.utils.clip_grad_norm_(amp.master_params(self.optimizer), cfg.train.max_grad_norm)
            else:
                loss.backward()
                if cfg.train.max_grad_norm > 0:
                    torch.nn.utils.clip_grad_norm_(self.biencoder.parameters(), cfg.train.max_grad_norm)

            if (i + 1) % cfg.train.gradient_accumulation_steps == 0:
                self.optimizer.step()
                scheduler.step()
                self.biencoder.zero_grad()

            lr = self.optimizer.param_groups[0]["lr"]

            if i % log_result_step == 0:
                logger.info(
                    "Epoch: %d: Step: %d/%d, loss=%f, lr=%f",
                    epoch,
                    data_iteration,
                    epoch_batches,
                    loss.item(),
                    lr,
                )

            if (i + 1) % rolling_loss_step == 0:
                logger.info("Train batch %d", data_iteration)
                latest_rolling_train_av_loss = rolling_train_loss / rolling_loss_step
                logger.info(
                    "Avg. loss per last %d batches: %f",
                    rolling_loss_step,
                    latest_rolling_train_av_loss,
                )
                rolling_train_loss = 0.0

            if data_iteration % eval_step == 0 and cfg.train.eval_during_epoch:
                logger.info(
                    "rank=%d, Validation: Epoch: %d Step: %d/%d",
                    cfg.local_rank,
                    epoch,
                    data_iteration,
                    epoch_batches,
                )
                self.validate_and_save(epoch, train_data_iterator.get_iteration(), scheduler, all_passages, save=False)
                self.biencoder.train()

        logger.info("Epoch finished on %d", cfg.local_rank)
        val_metrics: Dict[str, float] = self.validate_and_save(
            epoch, data_iteration, scheduler, all_passages, save=True
        )

        epoch_loss = (epoch_loss / epoch_batches) if epoch_batches > 0 else 0
        logger.info("Av Loss per epoch=%f", epoch_loss)
        logger.info("epoch total correct predictions=%d", epoch_correct_predictions)

        for key, value in val_metrics.items():
            metrics[key] = value

        metrics["Iteration"] = data_iteration
        metrics["Epoch"] = epoch
        metrics["Train loss"] = epoch_loss

        return metrics

    def _save_checkpoint(
        self,
        scheduler,
        epoch: int,
        offset: int,
        save_separate_models: bool = False,
        best_model_found: bool = False
    ) -> str:
        cfg = self.cfg
        model_to_save = get_model_obj(self.biencoder)
        if save_separate_models:
            # question encoder
            question_encoder = model_to_save.question_model
            question_encoder_cp = os.path.join(cfg.output_dir, f"dpr_question_encoder_epoch_{epoch}")
            question_encoder.save(question_encoder_cp)
            logger.info("Saved question encoder at %s", question_encoder_cp)

            # passage encoder
            passage_encoder = model_to_save.ctx_model
            passage_encoder_cp = os.path.join(cfg.output_dir, f"dpr_passage_encoder_epoch_{epoch}")
            passage_encoder.save(passage_encoder_cp)
            logger.info("Saved passage encoder at %s", passage_encoder_cp)

        if not best_model_found:
            cp = os.path.join(cfg.output_dir, cfg.checkpoint_file_name + "." + str(epoch))
        else:
            cp = os.path.join(cfg.output_dir, cfg.checkpoint_file_name + "_best.cp")

        # save tokenizer
        self.tensorizer.tokenizer.save_pretrained(cfg.output_dir)

        meta_params = get_encoder_params_state_from_cfg(cfg)
        state = CheckpointState(
            model_to_save.get_state_dict(),
            self.optimizer.state_dict(),
            scheduler.state_dict(),
            offset,
            epoch,
            meta_params,
        )
        torch.save(state._asdict(), cp)
        logger.info("Saved checkpoint at %s", cp)
        return cp

    def _load_saved_state(self, saved_state: CheckpointState):
        epoch = saved_state.epoch
        # offset is currently ignored since all checkpoints are made after full epochs
        offset = saved_state.offset
        if offset == 0:  # epoch has been completed
            epoch += 1
        logger.info("Loading checkpoint @ batch=%s and epoch=%s", offset, epoch)

        if self.cfg.ignore_checkpoint_offset:
            self.start_epoch = 0
            self.start_batch = 0
        else:
            self.start_epoch = epoch
            # TODO: offset doesn't work for multiset currently
            self.start_batch = 0  # offset

        model_to_load = get_model_obj(self.biencoder)
        logger.info("Loading saved model state ...")

        model_to_load.load_state(saved_state)

        if not self.cfg.ignore_checkpoint_optimizer:
            if saved_state.optimizer_dict:
                logger.info("Loading saved optimizer state ...")
                self.optimizer.load_state_dict(saved_state.optimizer_dict)

            if saved_state.scheduler_dict:
                self.scheduler_state = saved_state.scheduler_dict


def _calc_loss(
    cfg,
    loss_function,
    local_q_vector,
    local_ctx_vectors,
    local_positive_idxs,
    local_hard_negatives_idxs: list = None,
    loss_scale: float = None,
) -> Tuple[T, bool]:
    """
    Calculates In-batch negatives schema loss and supports to run it in DDP mode by exchanging the representations
    across all the nodes.
    """
    distributed_world_size = cfg.distributed_world_size or 1
    if distributed_world_size > 1:
        q_vector_to_send = torch.empty_like(local_q_vector).cpu().copy_(local_q_vector).detach_()
        ctx_vector_to_send = torch.empty_like(local_ctx_vectors).cpu().copy_(local_ctx_vectors).detach_()

        global_question_ctx_vectors = all_gather_list(
            [
                q_vector_to_send,
                ctx_vector_to_send,
                local_positive_idxs,
                local_hard_negatives_idxs,
            ],
            max_size=cfg.global_loss_buf_sz,
        )

        global_q_vector = []
        global_ctxs_vector = []

        # ctxs_per_question = local_ctx_vectors.size(0)
        positive_idx_per_question = []
        hard_negatives_per_question = []

        total_ctxs = 0

        for i, item in enumerate(global_question_ctx_vectors):
            q_vector, ctx_vectors, positive_idx, hard_negatives_idxs = item

            if i != cfg.local_rank:
                global_q_vector.append(q_vector.to(local_q_vector.device))
                global_ctxs_vector.append(ctx_vectors.to(local_q_vector.device))
                positive_idx_per_question.extend([v + total_ctxs for v in positive_idx])
                hard_negatives_per_question.extend([[v + total_ctxs for v in l] for l in hard_negatives_idxs])
            else:
                global_q_vector.append(local_q_vector)
                global_ctxs_vector.append(local_ctx_vectors)
                positive_idx_per_question.extend([v + total_ctxs for v in local_positive_idxs])
                hard_negatives_per_question.extend([[v + total_ctxs for v in l] for l in local_hard_negatives_idxs])
            total_ctxs += ctx_vectors.size(0)
        global_q_vector = torch.cat(global_q_vector, dim=0)
        global_ctxs_vector = torch.cat(global_ctxs_vector, dim=0)

    else:
        global_q_vector = local_q_vector
        global_ctxs_vector = local_ctx_vectors
        positive_idx_per_question = local_positive_idxs
        hard_negatives_per_question = local_hard_negatives_idxs

    loss, is_correct = loss_function.calc(
        global_q_vector,
        global_ctxs_vector,
        positive_idx_per_question,
        hard_negatives_per_question,
        loss_scale=loss_scale,
    )

    return loss, is_correct


def _do_biencoder_fwd_pass(
    model: nn.Module,
    input: BiEncoderBatch,
    tensorizer: Tensorizer,
    cfg,
    encoder_type: str,
    rep_positions=0,
    loss_scale: float = None,
) -> Tuple[torch.Tensor, int]:

    input = BiEncoderBatch(**move_to_device(input._asdict(), cfg.device))

    q_attn_mask = tensorizer.get_attn_mask(input.question_ids)
    ctx_attn_mask = tensorizer.get_attn_mask(input.context_ids)

    if model.training:
        model_out = model(
            input.question_ids,
            input.question_segments,
            q_attn_mask,
            input.context_ids,
            input.ctx_segments,
            ctx_attn_mask,
            encoder_type=encoder_type,
            representation_token_pos=rep_positions,
        )
    else:
        with torch.no_grad():
            model_out = model(
                input.question_ids,
                input.question_segments,
                q_attn_mask,
                input.context_ids,
                input.ctx_segments,
                ctx_attn_mask,
                encoder_type=encoder_type,
                representation_token_pos=rep_positions,
            )

    local_q_vector, local_ctx_vectors = model_out

    loss_function = BiEncoderNllLoss()

    loss, is_correct = _calc_loss(
        cfg,
        loss_function,
        local_q_vector,
        local_ctx_vectors,
        input.is_positive,
        input.hard_negatives,
        loss_scale=loss_scale,
    )

    is_correct = is_correct.sum().item()

    if cfg.n_gpu > 1:
        loss = loss.mean()
    if cfg.train.gradient_accumulation_steps > 1:
        loss = loss / cfg.train.gradient_accumulation_steps
    return loss, is_correct


@hydra.main(config_path="conf", config_name="biencoder_train_cfg")
def main(cfg: DictConfig):
    wandb.login(key=os.environ["WANDB_API_KEY"])

    wandb.init(project="dpr",
               entity="ask-ai",
               name=f"dpr_lr-{cfg.train.learning_rate}_bs-{cfg.train.batch_size}_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}",
               config=flatten_dict(cfg),
               mode="disabled" if not cfg.wandb_logs else None,
   )

    if cfg.train.gradient_accumulation_steps < 1:
        raise ValueError(
            "Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                cfg.train.gradient_accumulation_steps
            )
        )

    if cfg.output_dir is not None:
        os.makedirs(cfg.output_dir, exist_ok=True)

    cfg = setup_cfg_gpu(cfg)
    set_seed(cfg)

    if cfg.local_rank in [-1, 0]:
        logger.info("CFG (after gpu  configuration):")
        logger.info("%s", OmegaConf.to_yaml(cfg))

    trainer = BiEncoderTrainer(cfg)

    if cfg.train_datasets and len(cfg.train_datasets) > 0:
        trainer.run_train()
    elif cfg.model_file and cfg.dev_datasets:
        logger.info("No train files are specified. Run 2 types of validation for specified model file")
        trainer.validate_nll()
        trainer.validate_average_rank()
    else:
        logger.warning("Neither train_file or (model_file & dev_file) parameters are specified. Nothing to do.")


if __name__ == "__main__":
    logger.info("Sys.argv: %s", sys.argv)
    hydra_formatted_args = []
    # convert the cli params added by torch.distributed.launch into Hydra format
    for arg in sys.argv:
        if arg.startswith("--"):
            hydra_formatted_args.append(arg[len("--") :])
        else:
            hydra_formatted_args.append(arg)
    logger.info("Hydra formatted Sys.argv: %s", hydra_formatted_args)
    sys.argv = hydra_formatted_args

    main()
