import os
import json
import requests
import tiktoken
from tqdm import tqdm
import torch
import torch.nn as nn
from torch.nn import functional as F
from transformers import GPT2LMHeadModel

# -----------------------------------------------------------------------------
hellaswags = {
    "train": "https://raw.githubusercontent.com/rowanz/hellaswag/master/data/hellaswag_train.jsonl",
    "val": "https://raw.githubusercontent.com/rowanz/hellaswag/master/data/hellaswag_val.jsonl",
    "test": "https://raw.githubusercontent.com/rowanz/hellaswag/master/data/hellaswag_test.jsonl",
}

DATA_CACHE_DIR = os.path.join(os.path.dirname(__file__), "data")
enc = tiktoken.get_encoding("gpt2")

def download_file(url: str, fname: str, chunk_size=1024):
    """Helper function to download a file from a given url"""
    resp = requests.get(url, stream=True)
    total = int(resp.headers.get("content-length", 0))
    with open(fname, "wb") as file, tqdm(
        desc=fname,
        total=total,
        unit="iB",
        unit_scale=True,
        unit_divisor=1024,
    ) as bar:
        for data in resp.iter_content(chunk_size=chunk_size):
            size = file.write(data)
            bar.update(size)

def download(split):
    """Downloads HellaSwag DATA_CACHE_DIR"""
    os.makedirs(DATA_CACHE_DIR, exist_ok=True)
    data_url = hellaswags[split]
    data_filename = os.path.join(DATA_CACHE_DIR, f"hellaswag_{split}.jsonl")
    if not os.path.exists(data_filename):
        print(f"Downloading {data_url} to {data_filename}...")
        download_file(data_url, data_filename)

def render_example(example):
    """
    Given the example as a dictionary, render it as three torch tensors:
    - tokens (the tokens of context + completion, of size 4xN, as there are always 4 candidates)
    - mask (is 1 in the region of the candidate completion, where we evaluate likelihoods)
    - label (the index of the correct completion, which we hope has the highest likelihood)
    """
    ctx = example["ctx"]
    label = example["label"]
    endings = example["endings"]

    # data needed to reproduce this eval on the C size
    data = {
        "label": label,
        "ctx_tokens": None,
        "ending_tokens": [],
    }

    # gather up all the tokens
    ctx_tokens = enc.encode(ctx)
    data["ctx_tokens"] = ctx_tokens
    tok_rows = []
    mask_rows = []
    for end in endings:
        end_tokens = enc.encode(" " + end) # note: prepending " " because GPT-2 tokenizer -- may be treated as a sentence start and get a special token
        tok_rows.append(ctx_tokens + end_tokens)
        mask_rows.append([0]*len(ctx_tokens) + [1]*len(end_tokens))
        data["ending_tokens"].append(end_tokens)

    # have to be careful during the collation because the number of tokens in each row can differ
    max_len = max(len(row) for row in tok_rows)
    tokens = torch.zeros((4, max_len), dtype=torch.long)
    mask = torch.zeros((4, max_len), dtype=torch.long)
    for i, (tok_row, mask_row) in enumerate(zip(tok_rows, mask_rows)):
        tokens[i, :len(tok_row)] = torch.tensor(tok_row)
        mask[i, :len(mask_row)] = torch.tensor(mask_row)

    return data, tokens, mask, label

def iterate_examples(split):
    # there are 10,042 examples in total in val
    download(split)
    with open(os.path.join(DATA_CACHE_DIR, f"hellaswag_{split}.jsonl"), "r") as f:
        for line in f:
            example = json.loads(line)
            yield example

@torch.no_grad()
def evaluate(model_type, device):

    torch.set_float32_matmul_precision('high') # use tf32
    model = GPT2LMHeadModel.from_pretrained(model_type)
    model.to(device)
    # model = torch.compile(model) # optionally torch compile the model

    num_correct_norm = 0
    num_correct = 0
    num_total = 0
    for example in iterate_examples("val"):
        data, tokens, mask, label = render_example(example)
        tokens = tokens.to(device)
        mask = mask.to(device)

        # get the logits
        logits = model(tokens).logits # size: sentences x tokens x vocab
        # evaluate the autoregressive loss at all positions
        # model does not output the logits for the first token, it is treated as the holy ground truth
        # so we have to cut the first token -- its got not corresponding logits (cause we are not getting logits for the "empty start most probable token")
        # and we have to cut the last token -- we dont care about further tokens, only want to evaluate loss for the ones given
        # contiguous() is used to ensure that the memory is laid out in a contiguous block, it returns a copy if tensor is not contiguous or self otherwise
        # why do we need to call contiguous() explicitly?
        # 1. You control when the overhead of making a contiguous copy happens, it is not hidden somewhere in automatic contiguity checks
        # 2. You avoid repeated automatic contiguous() if you use the non-contiguous tensor multiple times in your code
        # 3. Some operations yield errors or unexpected results on non-contiguous tensors (for some reason they are not automatically checked)
        # (according to Claude transpose+view can be problematic -- I checked it in the hellaswag_playground and it seems to work fine)
        #   ways to check if copy: original.data_ptr() == contiguous.data_ptr() or id(original) == id(contiguous)
        # ... keeps all preceding dimensions, : keeps all elements in the last dimension
        shift_logits = (logits[..., :-1, :]).contiguous() # size: sentences x (tokens-1) x vocab
        shift_tokens = (tokens[..., 1:]).contiguous()     # size: sentences x (tokens-1)
        # view is more efficient than reshape, it does not create a copy of the tensor
        # view(-1) -- automatically calculates the size of the only dimension for the view so sentences x tokens x vocab becomes sentences*tokens*vocab
        # view(-1, shift_logits.size(-1)) -- automatically calculates the size of the first dimension for the view so sentences*tokens x vocab
        # here we produce a smacked together matrix of all the logits, and a smacked together vector of all the tokens
        flat_shift_logits = shift_logits.view(-1, shift_logits.size(-1)) # size: (sentences*tokens) x vocab
        flat_shift_tokens = shift_tokens.view(-1)                        # size: (sentences*tokens)
        # for each entry in the flat_shift_tokens, we have a corresponding entry in the flat_shift_logits
        #    def cross_entropy(t, target, weights: dict | None = None):
        #        return -1 * torch.log(torch.exp(t[target]) / torch.exp(t).sum())
        # we evaluate the loss for each of these pairs, hence reduction='none' -- each pair only cares about itself
        # then we view this flat losses tensor back to the "human" shape of sentences x tokens
        shift_losses = F.cross_entropy(flat_shift_logits, flat_shift_tokens, reduction='none')
        shift_losses = shift_losses.view(tokens.size(0), -1) # size: sentences x tokens
        # now get the average loss just for the completion region (where mask == 1), in each row
        # we multiply the losses by the mask, so that the losses outside the completion region are zeroed out 
        # cause we only care about the completion loss values
        shift_mask = (mask[..., 1:]).contiguous() # size: sentences x (tokens-1)
        masked_shift_losses = shift_losses * shift_mask
        # sum and divide by the number of 1s in the mask
        sum_loss = masked_shift_losses.sum(dim=1)
        avg_loss = sum_loss / shift_mask.sum(dim=1)
        # now we have a loss for each of the 4 completions
        # the one with the lowest loss should be the most likely
        pred = sum_loss.argmin().item()
        pred_norm = avg_loss.argmin().item()

        # accumulate stats
        num_total += 1
        num_correct += int(pred == label)
        num_correct_norm += int(pred_norm == label)
        # accuracy_norm means that we are comparing the average loss of the completions
        # ie we weight the completions by their length
        # otherwise the longer completions would have higher loss sometimes just because they have more tokens = more loss to sum
        print(f"{num_total} acc_norm: {num_correct_norm}/{num_total}={num_correct_norm/num_total:.4f}")

        # debug: pretty print a few examples, and the losses in each case
        if num_total < 10:
            print("---")
            print(f"Context:\n {example['ctx']}")
            print(f"Endings:")
            for i, end in enumerate(example["endings"]):
                print(f"{i} (loss: {avg_loss[i].item():.4f}) {end}")
            print(f"predicted: {pred_norm}, actual: {label}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--model_type", type=str, default="gpt2", help="the model type to use")
    parser.add_argument("-d", "--device", type=str, default="cuda", help="the device to use")
    args = parser.parse_args()
    evaluate(args.model_type, args.device)