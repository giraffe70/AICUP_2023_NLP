###  PART 2. Sentence retrieval ###

# ===== Import some libs =====
# built-in libs
from pathlib import Path
from typing import Dict, List, Set, Tuple, Union

# third-party libs
import numpy as np
import pandas as pd
from pandarallel import pandarallel
from sklearn.model_selection import train_test_split
from tqdm.auto import tqdm

import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    BertTokenizer,
)
from dataset import BERTDataset, Dataset

# local libs
from utils import (
    generate_evidence_to_wiki_pages_mapping,
    jsonl_dir_to_df,
    load_json,
    load_model,
    save_checkpoint,
    set_lr_scheduler,
)
import json

pandarallel.initialize(progress_bar=True, verbose=0, nb_workers=10)
# ============================

# Global variable
page_num = 7 # 5
SEED = 42
TOP_N = 5

# ===== Preload wiki database =====
# TRAIN_DATA = load_json("data/public_train.jsonl")
# TEST_DATA = load_json("data/public_test.jsonl")
TRAIN_DATA = load_json("data/public_train_all.jsonl")
TEST_DATA = load_json("data/public_private_test_all.jsonl")

DOC_DATA = load_json(f"data/train_doc{page_num}.jsonl")

LABEL2ID: Dict[str, int] = {"supports": 0, "refutes": 1, "NOT ENOUGH INFO": 2}
ID2LABEL: Dict[int, str] = {v: k for k, v in LABEL2ID.items()}

_y = [LABEL2ID[data["label"]] for data in TRAIN_DATA]

# GT means Ground Truth
TRAIN_GT, DEV_GT = train_test_split(
    DOC_DATA,
    test_size=0.2,
    random_state=SEED,
    shuffle=True,
    stratify=_y,
)

# ===== Preload wiki database =====
wiki_pages = jsonl_dir_to_df("data/wiki-pages")
mapping = generate_evidence_to_wiki_pages_mapping(wiki_pages)
del wiki_pages
# =================================


# ========== Helper function ==========

# Calculate precision for sentence retrieval
def evidence_macro_precision(instance: Dict, top_rows: pd.DataFrame) -> Tuple[float, float]:
    """Calculate precision for sentence retrieval
    This function is modified from fever-scorer.
    https://github.com/sheffieldnlp/fever-scorer/blob/master/src/fever/scorer.py
    """
    this_precision = 0.0
    this_precision_hits = 0.0

    # Return 0, 0 if label is not enough info since not enough info does not
    # contain any evidence.
    if instance["label"].upper() != "NOT ENOUGH INFO":
        # e[2] is the page title, e[3] is the sentence index
        all_evi = [[e[2], e[3]]
                   for eg in instance["evidence"]
                   for e in eg
                   if e[3] is not None]
        claim = instance["claim"]
        predicted_evidence = top_rows[top_rows["claim"] == claim]["predicted_evidence"].tolist()

        for prediction in predicted_evidence:
            if prediction in all_evi:
                this_precision += 1.0
            this_precision_hits += 1.0

        return (this_precision / this_precision_hits) if this_precision_hits > 0 else 1.0, 1.0

    return 0.0, 0.0


# Calculate recall for sentence retrieval
def evidence_macro_recall(instance: Dict, top_rows: pd.DataFrame) -> Tuple[float, float]:
    """Calculate recall for sentence retrieval
    This function is modified from fever-scorer.
    https://github.com/sheffieldnlp/fever-scorer/blob/master/src/fever/scorer.py
    """
    # We only want to score F1/Precision/Recall of recalled evidence for NEI claims
    if instance["label"].upper() != "NOT ENOUGH INFO":
        # If there's no evidence to predict, return 1
        if len(instance["evidence"]) == 0 or all(
            [len(eg) == 0 for eg in instance]):
            return 1.0, 1.0

        claim = instance["claim"]

        predicted_evidence = top_rows[top_rows["claim"] == claim]["predicted_evidence"].tolist()

        for evidence_group in instance["evidence"]:
            evidence = [[e[2], e[3]] for e in evidence_group]
            if all([item in predicted_evidence for item in evidence]):
                # We only want to score complete groups of evidence. 
                # Incomplete groups are worthless.
                return 1.0, 1.0
        return 0.0, 1.0
    return 0.0, 0.0

# Calculate the scores of sentence retrieval
def evaluate_retrieval(
    probs: np.ndarray,
    df_evidences: pd.DataFrame,
    ground_truths: pd.DataFrame,
    top_n: int = 5, # 7,
    cal_scores: bool = True,
    save_name: str = None,
) -> Dict[str, float]:

    # Calculate the scores of sentence retrieval
    df_evidences["prob"] = probs
    top_rows = (
        df_evidences.groupby("claim").apply(lambda x: x.nlargest(top_n, "prob")).reset_index(drop=True)
    )

    if cal_scores:
        macro_precision = 0
        macro_precision_hits = 0
        macro_recall = 0
        macro_recall_hits = 0

        for i, instance in enumerate(ground_truths):
            macro_prec = evidence_macro_precision(instance, top_rows)
            macro_precision += macro_prec[0]
            macro_precision_hits += macro_prec[1]

            macro_rec = evidence_macro_recall(instance, top_rows)
            macro_recall += macro_rec[0]
            macro_recall_hits += macro_rec[1]

        pr = (macro_precision / macro_precision_hits) if macro_precision_hits > 0 else 1.0
        rec = (macro_recall / macro_recall_hits) if macro_recall_hits > 0 else 0.0
        f1 = 2.0 * pr * rec / (pr + rec)

    if save_name is not None:
        # write doc7_sent5 file
        with open(f"data/{save_name}", "w") as f:
            for instance in ground_truths:
                claim = instance["claim"]
                predicted_evidence = top_rows[top_rows["claim"] == claim]["predicted_evidence"].tolist()
                instance["predicted_evidence"] = predicted_evidence
                f.write(json.dumps(instance, ensure_ascii=False) + "\n")

    if cal_scores:
        return {"F1 score": f1, "Precision": pr, "Recall": rec}


# Inference script to get probabilites for the candidate evidence sentences
def get_predicted_probs(model: nn.Module, dataloader: Dataset,device: torch.device) -> np.ndarray:
    """
    Args:
      model: the one from HuggingFace Transformers
      dataloader: devset or testset in torch dataloader
    Returns:
      np.ndarray: probabilites of the candidate evidence sentences
    """
    model.eval()
    probs = []

    with torch.no_grad():
        for batch in tqdm(dataloader):
            batch = {k: v.to(device) for k, v in batch.items()}
            outputs = model(**batch)
            logits = outputs.logits
            probs.extend(torch.softmax(logits, dim=1)[:, 1].tolist())

    return np.array(probs)


# AicupTopkEvidenceBERTDataset class for AICUP dataset with top-k evidence sentences
class SentRetrievalBERTDataset(BERTDataset):
    def __getitem__(self, idx: int, **kwargs,) -> Tuple[Dict[str, torch.Tensor], int]:
        item = self.data.iloc[idx]
        sentA = item["claim"]
        sentB = item["text"]
        # claim [SEP] text
        concat = self.tokenizer(
            sentA,
            sentB,
            padding="max_length",
            max_length=self.max_length,
            truncation=True,
        )
        concat_ten = {k: torch.tensor(v) for k, v in concat.items()}
        if "label" in item:
            concat_ten["labels"] = torch.tensor(item["label"])
        return concat_ten

# Main function for sentence retrieval
# Only for creating train sentences.
def pair_with_wiki_sentences(mapping: Dict[str, Dict[int, str]], df: pd.DataFrame, negative_ratio: float) -> pd.DataFrame:
    claims = []
    sentences = []
    labels = []
    # positive
    for i in range(len(df)):
        if df["label"].iloc[i] == "NOT ENOUGH INFO":
            continue
        claim = df["claim"].iloc[i]
        evidence_sets = df["evidence"].iloc[i]
        for evidence_set in evidence_sets:
            sents = []
            for evidence in evidence_set:
                # evidence[2] is the page title
                page = evidence[2].replace(" ", "_")
                # the only page with weird name
                if page == "臺灣海峽危機#第二次臺灣海峽危機（1958）":
                    continue
                # evidence[3] is in form of int however, mapping requires str
                sent_idx = str(evidence[3])
                sents.append(mapping[page][sent_idx])
            whole_evidence = " ".join(sents)
            claims.append(claim)
            sentences.append(whole_evidence)
            labels.append(1)

    # negative
    for i in range(len(df)):
        if df["label"].iloc[i] == "NOT ENOUGH INFO":
            continue
        claim = df["claim"].iloc[i]

        evidence_set = set([(evidence[2], evidence[3])
                            for evidences in df["evidence"][i]
                            for evidence in evidences])
        predicted_pages = df["predicted_pages"][i]
        for page in predicted_pages:
            page = page.replace(" ", "_")
            try:
                page_sent_id_pairs = [
                    (page, sent_idx) for sent_idx in mapping[page].keys()
                ]
            except KeyError:
                # print(f"{page} is not in our Wiki db.")
                continue

            for pair in page_sent_id_pairs:
                if pair in evidence_set:
                    continue
                text = mapping[page][pair[1]]
                # `np.random.rand(1) <= 0.05`: Control not to add too many negative samples
                if text != "" and np.random.rand(1) <= negative_ratio:
                    claims.append(claim)
                    sentences.append(text)
                    labels.append(0)

    return pd.DataFrame({"claim": claims, "text": sentences, "label": labels})


# Only for creating dev and test sentences.
def pair_with_wiki_sentences_eval(mapping: Dict[str, Dict[int, str]], df: pd.DataFrame, is_testset: bool = False) -> pd.DataFrame:
    claims = []
    sentences = []
    evidence = []
    predicted_evidence = []

    # negative
    for i in range(len(df)):
        # if df["label"].iloc[i] == "NOT ENOUGH INFO":
        #     continue
        claim = df["claim"].iloc[i]

        predicted_pages = df["predicted_pages"][i]
        for page in predicted_pages:
            page = page.replace(" ", "_")
            try:
                page_sent_id_pairs = [(page, k) for k in mapping[page]]
            except KeyError:
                # print(f"{page} is not in our Wiki db.")
                continue

            for page_name, sentence_id in page_sent_id_pairs:
                text = mapping[page][sentence_id]
                if text != "":
                    claims.append(claim)
                    sentences.append(text)
                    if not is_testset:
                        evidence.append(df["evidence"].iloc[i])
                    predicted_evidence.append([page_name, int(sentence_id)])

    return pd.DataFrame({
        "claim": claims,
        "text": sentences,
        "evidence": evidence if not is_testset else None,
        "predicted_evidence": predicted_evidence,
    })


### Step 1. Setup training environment

# ===== params =====
# MODEL_NAME = "bert-base-chinese"
# MODEL_NAME = "hfl/chinese-roberta-wwm-ext"
MODEL_NAME = "hfl/chinese-bert-wwm-ext"
LR = 2e-5  
NUM_EPOCHS = 2  # 1
TRAIN_BATCH_SIZE = 32  # 64
TEST_BATCH_SIZE = 32  # 64
NEGATIVE_RATIO = 0.03 
VALIDATION_STEP = 25 # 50
TOP_N = 5  # 7
# ==================

# Experiment Directory
EXP_DIR = f"sent_retrieval/e{NUM_EPOCHS}_bs{TRAIN_BATCH_SIZE}_{LR}_neg{NEGATIVE_RATIO}_top{TOP_N}"
LOG_DIR = "logs/" + EXP_DIR
CKPT_DIR = "checkpoints/" + EXP_DIR
if not Path(LOG_DIR).exists():
    Path(LOG_DIR).mkdir(parents=True)
if not Path(CKPT_DIR).exists():
    Path(CKPT_DIR).mkdir(parents=True)

# ===== Step 2. Combine claims and evidences =====
train_df = pair_with_wiki_sentences(mapping, pd.DataFrame(TRAIN_GT), NEGATIVE_RATIO)
print("Now using the following train data with 0 (Negative) and 1 (Positive)")
counts = train_df["label"].value_counts()
print(counts)
dev_evidences = pair_with_wiki_sentences_eval(mapping, pd.DataFrame(DEV_GT))
# ================================================

# ===== Step 3. Start training =====
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

train_dataset = SentRetrievalBERTDataset(train_df, tokenizer=tokenizer)
val_dataset = SentRetrievalBERTDataset(dev_evidences, tokenizer=tokenizer)

train_dataloader = DataLoader(
    train_dataset,
    shuffle=True,
    batch_size=TRAIN_BATCH_SIZE,
)
eval_dataloader = DataLoader(val_dataset, batch_size=TEST_BATCH_SIZE)
# ==================================

# Save your memory.
del train_df

# ========== Trainer ==========
device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)
model.to(device)

optimizer = AdamW(model.parameters(), lr=LR)
num_training_steps = NUM_EPOCHS * len(train_dataloader)
lr_scheduler = set_lr_scheduler(optimizer, num_training_steps)

writer = SummaryWriter(LOG_DIR)

# Please make sure that you are using gpu when training
progress_bar = tqdm(range(num_training_steps))
current_steps = 0

for epoch in range(NUM_EPOCHS):
    model.train()

    for batch in train_dataloader:
        batch = {k: v.to(device) for k, v in batch.items()}
        outputs = model(**batch)
        loss = outputs.loss
        loss.backward()

        optimizer.step()
        lr_scheduler.step()
        optimizer.zero_grad()
        progress_bar.update(1)
        writer.add_scalar("training_loss", loss.item(), current_steps)

        y_pred = torch.argmax(outputs.logits, dim=1).tolist()
        y_true = batch["labels"].tolist()

        current_steps += 1

        if current_steps % VALIDATION_STEP == 0 and current_steps > 0:
            print("Start validation")
            probs = get_predicted_probs(model, eval_dataloader, device)

            val_results = evaluate_retrieval(
                probs=probs,
                df_evidences=dev_evidences,
                ground_truths=DEV_GT,
                top_n=TOP_N,
            )
            print(val_results)

            # log each metric separately to TensorBoard
            for metric_name, metric_value in val_results.items():
                writer.add_scalar(
                    f"dev_{metric_name}",
                    metric_value,
                    current_steps,
                )

            save_checkpoint(model, CKPT_DIR, current_steps)

print("Finished training!")
# =============================

# ===== Validation part =====
ckpt_name = "model.350.pt"   # change the ckpt_name 
model = load_model(model, ckpt_name, CKPT_DIR)
print("Start final evaluations and write prediction files.")

train_evidences = pair_with_wiki_sentences_eval(
    mapping=mapping,
    df=pd.DataFrame(TRAIN_GT),
)
train_set = SentRetrievalBERTDataset(train_evidences, tokenizer)
train_dataloader = DataLoader(train_set, batch_size=TEST_BATCH_SIZE)

print("Start calculating training scores")
probs = get_predicted_probs(model, train_dataloader, device)
train_results = evaluate_retrieval(
    probs=probs,
    df_evidences=train_evidences,
    ground_truths=TRAIN_GT,
    top_n=TOP_N,
    save_name=f"train_doc{page_num}sent{TOP_N}.jsonl",
)
print(f"Training scores => {train_results}")

print("Start validation")
probs = get_predicted_probs(model, eval_dataloader, device)
val_results = evaluate_retrieval(
    probs=probs,
    df_evidences=dev_evidences,
    ground_truths=DEV_GT,
    top_n=TOP_N,
    save_name=f"dev_doc{page_num}sent{TOP_N}.jsonl",
)

print(f"Validation scores => {val_results}")
# ===========================

# ===== Step 4. Check on our test data =====
test_data = load_json(f"data/test_doc{page_num}.jsonl")

test_evidences = pair_with_wiki_sentences_eval(
    mapping,
    pd.DataFrame(test_data),
    is_testset=True,
)
test_set = SentRetrievalBERTDataset(test_evidences, tokenizer)
test_dataloader = DataLoader(test_set, batch_size=TEST_BATCH_SIZE)

print("Start predicting the test data")
probs = get_predicted_probs(model, test_dataloader, device)
evaluate_retrieval(
    probs=probs,
    df_evidences=test_evidences,
    ground_truths=test_data,
    top_n=TOP_N,
    cal_scores=False,
    save_name=f"test_doc{page_num}sent{TOP_N}.jsonl",
)
