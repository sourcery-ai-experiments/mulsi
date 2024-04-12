"""Simple classifier training script.

Run with:
```
poetry run python -m scripts.train_clf
```
"""

import argparse

import torch
import wandb
from datasets import Features, Image, Value, load_dataset
from huggingface_hub import HfApi
from torch.utils.data import DataLoader
from transformers import CLIPModel, CLIPProcessor

from scripts.constants import ASSETS_FOLDER, HF_TOKEN, WANDB_API_KEY
from scripts.utils.clf import CLF

####################
# HYPERPARAMETERS
####################
parser = argparse.ArgumentParser("train-clf")
parser.add_argument(
    "--model_name", type=str, default="openai/clip-vit-base-patch32"
)
parser.add_argument(
    "--dataset_name", type=str, default="Xmaster6y/fruit-vegetable-concepts"
)
parser.add_argument("--download_dataset", action="store_true", default=False)
parser.add_argument("--batch_size", type=int, default=64)
parser.add_argument("--n_epochs", type=int, default=3)
parser.add_argument("--lr", type=float, default=1e-5)
####################

ARGS = parser.parse_args()
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[INFO] Running on {DEVICE}")
hf_api = HfApi(token=HF_TOKEN)
wandb.login(key=WANDB_API_KEY)  # type: ignore

processor = CLIPProcessor.from_pretrained(ARGS.model_name)
model = CLIPModel.from_pretrained(ARGS.model_name)
model.eval()
model.to(DEVICE)
for param in model.parameters():
    param.requires_grad = False

if ARGS.download_dataset:
    hf_api.snapshot_download(
        repo_id=ARGS.dataset_name,
        repo_type="dataset",
        local_dir=f"{ASSETS_FOLDER}/{ARGS.dataset_name}",
    )
features = Features({"image": Image(), "class": Value(dtype="string")})
dataset = load_dataset(
    f"{ASSETS_FOLDER}/{ARGS.dataset_name}", features=features
)
dataset = dataset.class_encode_column("class")


def collate_fn(batch):
    images, classes = zip(*[(x["image"], x["class"]) for x in batch])
    return images, classes


train_dataloader = DataLoader(
    dataset["train"],
    batch_size=ARGS.batch_size,
    shuffle=True,
    collate_fn=collate_fn,
)
val_dataloader = DataLoader(
    dataset["validation"],
    batch_size=ARGS.batch_size,
    shuffle=False,
    collate_fn=collate_fn,
)

clf = CLF(
    n_hidden=model.vision_model.config.hidden_size,
    classes=dataset["train"].features["class"].names,
)
clf.to(DEVICE)

with wandb.init(  # type: ignore
    project="mulsi-clf",
    config={
        **vars(ARGS),
    },
) as wandb_run:
    optimizer = torch.optim.Adam(clf.parameters(), lr=ARGS.lr)
    for epoch in range(ARGS.n_epochs):
        clf.train()
        for i, batch in enumerate(train_dataloader):
            images, classes = batch
            image_inputs = processor(
                images=images,
                return_tensors="pt",
            )
            image_inputs = {k: v.to(DEVICE) for k, v in image_inputs.items()}
            labels = torch.tensor(classes).to(DEVICE)
            optimizer.zero_grad()
            x = model.vision_model(**image_inputs)
            loss = clf.loss(x["pooler_output"], labels)
            loss.backward()
            optimizer.step()
            wandb.log({"train_loss": loss.item()})

        clf.eval()
        with torch.no_grad():
            val_loss = 0
            for i, batch in enumerate(val_dataloader):
                images, classes = batch
                image_inputs = processor(
                    images=images,
                    return_tensors="pt",
                )
                image_inputs = {
                    k: v.to(DEVICE) for k, v in image_inputs.items()
                }
                labels = torch.tensor(classes).to(DEVICE)
                x = model.vision_model(**image_inputs)
                loss = clf.loss(x["pooler_output"], labels)
                val_loss += loss.item()
            val_loss /= len(val_dataloader)
            wandb.log({"val_loss": val_loss})

    torch.save(clf.state_dict(), f"{ASSETS_FOLDER}/model.pt")

hf_api.upload_file(
    path_or_fileobj=f"{ASSETS_FOLDER}/model.pt",
    path_in_repo=f"data/{ARGS.model_name}",
    repo_id=ARGS.dataset_name.replace("concepts", "clfs"),
    repo_type="dataset",
)