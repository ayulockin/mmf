# Copyright (c) Facebook, Inc. and its affiliates.

import collections
import logging

import torch
from mmf.common.registry import registry
from mmf.common.sample import SampleList
from mmf.models.base_model import BaseModel
from mmf.modules.losses import MMFLoss
from mmf.utils.build import build_encoder
from mmf.utils.modeling import get_bert_configured_parameters
from torch import nn


logger = logging.getLogger()


@registry.register_model("vilt")
class ViLT(BaseModel):
    @classmethod
    def config_path(cls):
        return "configs/models/vilt/defaults.yaml"

    def build(self):
        self.text_embeddings = build_encoder(self.config.text_embeddings)
        self.image_embeddings = build_encoder(self.config.image_embeddings)
        self.encoder = build_encoder(self.config.image_encoder)

        # TODO: Add more classifiers later.
        self.heads = nn.ModuleDict()
        head_configs = self.config.get("heads", {})

        self.tasks = self.config.tasks
        if isinstance(self.tasks, str):
            self.tasks = self.tasks.split(",")

        self.prepare_itm = False
        self.prepare_mlm = False
        for task in self.tasks:
            head_config = head_configs[task]
            head_type = head_config.get("type", "mlp")
            if head_type == "itm":
                self.prepare_itm = True
            elif head_type == "mlm":
                self.prepare_mlm = True
            elif head_type == "mlm_itm":
                self.prepare_itm = True
                self.prepare_mlm = True
            head_class = registry.get_transformer_head_class(head_type)
            self.heads[task] = head_class(head_config)

        self.modality_keys = self.modality_type = ["text", "image"]

    def init_losses(self):
        self.losses = nn.ModuleDict()
        loss_configs = self.config.get("losses", {})
        for task in self.tasks:
            if task not in loss_configs:
                logger.warning(
                    f"No loss defined for {task}. Head is expected "
                    + "to return dict with 'losses'"
                )
                continue
            loss_config = loss_configs[task]
            self.losses[task] = MMFLoss(loss_config)

    def forward(self, sample_list):

        text_embedding = self.text_embeddings(sample_list)
        image_embedding = self.image_embeddings(sample_list)

        if self.prepare_itm:
            sample_list.itm_labels = self._infer_itm_labels(sample_list)
        if self.prepare_mlm:
            sample_list.mlm_labels = self._infer_mlm_labels(
                sample_list, image_embedding.size()[:-1]
            )
            self._encode_mlm(sample_list, image_embedding)

        # Feed through encoder
        embeddings = torch.cat([image_embedding, text_embedding], dim=1)
        attention_mask = self.get_attention_mask(
            sample_list, text_embedding, image_embedding
        )
        sequence, _ = self.encoder(embeddings, attention_mask=attention_mask)
        if sequence.dim() != 3:
            sequence = sequence.unsqueeze(1)

        outputs = self.heads[sample_list.dataset_name](
            sequence, processed_sample_list=sample_list
        )

        if isinstance(outputs, collections.MutableMapping) and "losses" in outputs:
            return outputs

        logits = outputs
        if isinstance(outputs, collections.MutableMapping) and "scores" in outputs:
            logits = outputs["scores"]
        logits = logits.contiguous().view(-1, logits.size(-1))
        output = self.losses[sample_list.dataset_name](sample_list, {"scores": logits})
        return {"losses": output, "scores": logits}

    def get_optimizer_parameters(self, config):
        if hasattr(self.encoder, "get_optimizer_parameters"):
            params = self.encoder.get_optimizer_parameters(config)
        else:
            params = [{"params": self.encoder.parameters()}]
        params += get_bert_configured_parameters(self.text_embeddings)
        params += get_bert_configured_parameters(self.heads)
        params += [{"params": self.image_embeddings.parameters()}]
        return params

    def get_attention_mask(self, sample_list, text_embedding, image_embedding):
        image_mask = getattr(sample_list, "image_mask", None)

        if image_mask is not None and sample_list.input_mask is not None:
            attention_mask = torch.cat((sample_list.input_mask, image_mask), dim=-1)
        elif image_mask is not None:
            text_mask = torch.ones(
                text_embedding.size()[:-1],
                dtype=text_embedding.dtype,
                device=text_embedding.device,
            )
            attention_mask = torch.cat((image_mask, text_mask), dim=-1)
        elif sample_list.input_mask is not None:
            image_mask = torch.ones(
                image_embedding.size()[:-1],
                dtype=image_embedding.dtype,
                device=image_embedding.device,
            )
            attention_mask = torch.cat((image_mask, sample_list.input_mask), dim=-1)
        else:
            attention_mask = None

        if attention_mask is not None:
            attention_mask = attention_mask.masked_fill(
                ~attention_mask.bool(), float("-inf")
            ).masked_fill(attention_mask.bool(), 0)
            attention_mask = attention_mask[:, None, None, :]

        return attention_mask

    def _infer_itm_labels(self, sample_list):
        input_ids = sample_list["input_ids"]
        itm_labels = {}
        if "is_correct" in sample_list:
            itm_labels["is_correct"] = sample_list["is_correct"]
        else:
            itm_labels["is_correct"] = torch.tensor(
                True, dtype=torch.long, device=input_ids.device
            )

        return itm_labels

    def _infer_mlm_labels(self, sample_list, image_embeddings_size):
        input_ids = sample_list["input_ids"]
        mlm_labels = {}
        current_text_idx = 0
        if "lm_label_ids" in sample_list:
            if sample_list["lm_label_ids"].dim() > 2:
                mlm_labels["text"] = sample_list["lm_label_ids"][:, current_text_idx]
                current_text_idx += 1
            else:
                mlm_labels["text"] = sample_list["lm_label_ids"]
        else:
            mlm_labels["text"] = torch.full(
                input_ids.size(),
                fill_value=-1,
                dtype=torch.long,
                device=input_ids.device,
            )
        mlm_labels["image"] = torch.full(
            image_embeddings_size,
            fill_value=-1,
            dtype=torch.long,
            device=input_ids.device,
        )
        mlm_labels["combined_labels"] = torch.cat(
            [mlm_labels["text"], mlm_labels["image"]], dim=-1
        )
        return mlm_labels

    def _encode_mlm(self, sample_list, image_embedding):
        assert "lm_label_ids" in sample_list

        masked_sample_list = SampleList()
        masked_sample_list.input_ids = sample_list.get(
            "input_ids_masked", sample_list.input_ids
        )
        masked_sample_list.segment_ids = sample_list.segment_ids
        text_embedding = self.text_embeddings(masked_sample_list)

        embeddings = torch.cat([image_embedding, text_embedding], dim=1)
        attention_mask = self.get_attention_mask(
            sample_list, text_embedding, image_embedding
        )
        sequence, _ = self.encoder(embeddings, attention_mask=attention_mask)
        if sequence.dim() != 3:
            sequence = sequence.unsqueeze(1)

        sample_list.hs_masked_for_mlm = sequence
