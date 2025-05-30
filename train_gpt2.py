from dataclasses import dataclass
import torch
import torch.nn as nn
from torch.nn import functional as F
import math
import time
import inspect
import os
class MLP(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.c_fc = nn.Linear(config.n_embd, 4 * config.n_embd)
        self.gelu = nn.GELU(approximate='tanh')
        self.c_proj = nn.Linear(4 * config.n_embd, config.n_embd)
    
    def forward(self,x):
        x = self.c_fc(x)
        x = self.gelu(x)
        x = self.c_proj(x)
        return x
class CausalSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        assert config.n_embd % config.n_head == 0
        self.c_attn = nn.Linear(config.n_embd,3 * config.n_embd)
        self.c_proj = nn.Linear(config.n_embd,config.n_embd)
        self.c_proj.NANOGPT_SCALE_INIT = 1
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        self.register_buffer("bias",torch.tril(torch.ones(config.block_size,config.block_size)).view(1,1,config.block_size,config.block_size))
    
    def forward(self,x):
        B,T,C = x.size()
        qkv = self.c_attn(x)
        q,k,v = qkv.split(self.n_embd,dim=2)
        k = k.view(B,T,self.n_head,C//self.n_head).transpose(1,2)
        q = q.view(B,T,self.n_head,C//self.n_head).transpose(1,2)
        v = v.view(B,T,self.n_head,C//self.n_head).transpose(1,2)
        #att = (q @ k.transpose(-2,-1)) * (1.0 / math.sqrt(k.size(-1)))
        #att = att.masked_fill(self.bias[:,:,:T,:T] == 0 ,float("-inf"))
        #att = F.softmax(att,dim=-1)
        #y = att @ v
        y = F.scaled_dot_product_attention(q,k,v,is_causal=True)
        y = y.transpose(1,2).contiguous().view(B,T,C)
        y = self.c_proj(y)
        return y

class Block(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.n_embd)
        self.attn = CausalSelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.n_embd)
        self.mlp = MLP(config)

    def forward(self,x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x

@dataclass
class GPTConfig:
    block_size:int = 1024
    vocab_size:int = 50257
    n_layer:int = 12
    n_head:int = 12
    n_embd:int = 768

class GPT(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config

        self.transformer = nn.ModuleDict(dict(
            wte = nn.Embedding(config.vocab_size,config.n_embd),
            wpe = nn.Embedding(config.block_size,config.n_embd),
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
            ln_f = nn.LayerNorm(config.n_embd),
        ))
        self.lm_head = nn.Linear(config.n_embd,config.vocab_size,bias=False)
        # weight sharing
        self.transformer.wte.weight = self.lm_head.weight

        # init params
        self.apply(self._init_weights)

    def _init_weights(self,module):
        std = 0.02
        if isinstance(module,nn.Linear):
            if hasattr(module,'NANOGPT_SCALE_INIT'):
                std *= (2 * self.config.n_layer) ** -0.5
            nn.init.normal_(module.weight,mean=0.0,std=std)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module,nn.Embedding):
            nn.init.normal_(module.weight,mean=0.0,std=std)
    def forward(self,idx,targets=None):
        B,T = idx.size()
        assert T <= self.config.block_size, f"cannot forward sequence length {T}, block size is {self.config.block_size}."
        pos = torch.arange(0,T,dtype=torch.long,device=idx.device)
        pos_emb = self.transformer.wpe(pos)
        tok_emb = self.transformer.wte(idx)
        x = tok_emb + pos_emb
        for block in self.transformer.h:
            x = block(x)
        x = self.transformer.ln_f(x)
        logits = self.lm_head(x)
        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.view(-1,logits.size(-1)),targets.view(-1))
        return logits, loss
    
    @classmethod
    def from_pretrained(cls,model_type):
        assert model_type in {'gpt2','gpt2-medium','gpt2-large','gpt2-xl'}
        from transformers import GPT2LMHeadModel
        print("Loading weights from pretrained GPT2: %s" % model_type)
        config_args = {
            'gpt2': dict(n_layer=12, n_head=12, n_embd=768),            #124M
            'gpt2-medium': dict(n_layer=24, n_head=16, n_embd=1024),    #350M
            'gpt2-large': dict(n_layer=36, n_head=20, n_embd=1280),     #774M
            'gpt2-xl': dict(n_layer=48, n_head=25, n_embd=1600)         #1558M
        }[model_type]
        config_args['vocab_size'] = 50257
        config_args['block_size'] = 1024
        config = GPTConfig(**config_args)
        model = GPT(config)
        sd = model.state_dict()
        sd_keys = sd.keys()
        sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')]

        model_hf = GPT2LMHeadModel.from_pretrained(model_type)
        sd_hf = model_hf.state_dict()
        sd_keys_hf = sd_hf.keys()
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias')]
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.bias')]
        transposed = ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']

        assert len(sd_keys_hf) == len(sd_keys), f"mismatched keys: {len(sd_keys_hf)} != {len(sd_keys)}"
        for k in sd_keys_hf:
            if any(k.endswith(w) for w in transposed):
                assert sd_hf[k].shape[::-1] == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k].t())
            else:
                assert sd_hf[k].shape == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k])
        return model
    def configure_optimizers(self,weight_decay,lr,device):
        param_dict = {pn:p for pn,p in self.named_parameters()}
        param_dict = {pn:p for pn,p in param_dict.items() if p.requires_grad}
        decay_params = [p for n,p in param_dict.items() if p.dim() >= 2]
        nodecay_params = [p for n,p in param_dict.items() if p.dim() < 2]
        optim_groups = [
            {'params': decay_params, 'weight_decay': weight_decay},
            {'params': nodecay_params, 'weight_decay': 0.0}
        ]
        num_decay_params = sum(p.numel() for p in decay_params)
        num_nodecay_params = sum(p.numel() for p in nodecay_params)
        print(f"num of decay param tensors: {len(decay_params)} with num of params: {num_decay_params}")
        print(f"num of nodecay param tensors: {len(nodecay_params)} with num of params: {num_nodecay_params}")
        fused_available = 'fused' in inspect.signature(torch.optim.AdamW).parameters
        use_fused = fused_available and 'cuda' in device
        print(f"use fused AdamW: {use_fused}")
        optimizer = torch.optim.AdamW(optim_groups,lr=lr,betas=(0.9,0.95),eps=1e-8,fused=use_fused)
        return optimizer
    

import tiktoken
import numpy as np
enc = tiktoken.get_encoding('gpt2')

def load_tokens(filename):
    npt = np.load(filename)
    ppt = torch.tensor(npt,dtype=torch.long)
    return ppt
class DataLoaderLite:
    def __init__(self,B,T,process_rank,num_processes,split):
        self.B = B
        self.T = T 
        self.process_rank = process_rank
        self.num_processes = num_processes
        assert split in {'train','val'}
        data_root = "/data1/xlyang/edu_fineweb10B"
        shards = os.listdir(data_root)
        shards = [s for s in shards if split in s]
        shards = sorted(shards)
        shards = [os.path.join(data_root,s) for s in shards]
        self.shards = shards
        assert len(shards) > 0, f"no shards found for split {split}"
        if master_process:
            print(f"found {len(shards)} shards for split {split}")
        #with open('./data/input.txt','r') as f:
        #    text = f.read()
        #enc = tiktoken.get_encoding('gpt2')
        #tokens = enc.encode(text)
        #self.tokens = torch.tensor(tokens)
        self.current_shard = 0
        self.tokens = load_tokens(self.shards[self.current_shard])

        #if master_process:
        #   print(f"loaded {len(self.tokens)} tokens.")
        #    print(f"1 epoch = {len(self.tokens) // (B*T)} batches.")

        self.current_position = self.B * self.T * self.process_rank
        self.reset()
    def reset(self):
        self.current_shard = 0
        self.tokens = load_tokens(self.shards[self.current_shard])
        self.current_position = self.B * self.T * self.process_rank
    def next_batch(self):
        B, T =self.B,self.T
        buf = self.tokens[self.current_position:self.current_position+B*T+1]
        x = (buf[:-1]).view(B,T)
        y = (buf[1:]).view(B,T)
        self.current_position += B * T * self.num_processes
        if self.current_position + (B*T*self.num_processes+1) > len(self.tokens):
            self.current_shard = (self.current_shard + 1) % len(self.shards)
            self.tokens = load_tokens(self.shards[self.current_shard])
            self.current_position = self.B * self.T * self.process_rank
        return x,y

from torch.distributed import init_process_group, destroy_process_group
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist
ddp = int(os.environ.get('RANK',-1)) != -1
if ddp:
    assert torch.cuda.is_available(), "for now we need CUDA for ddp"
    init_process_group(backend='nccl')
    ddp_rank = int(os.environ['RANK'])
    ddp_local_rank = int(os.environ['LOCAL_RANK'])
    ddp_world_size = int(os.environ['WORLD_SIZE'])
    device = f'cuda:{ddp_local_rank}'
    torch.cuda.set_device(device)
    master_process = ddp_rank == 0 # this process will do logging, checkpoints etc.
else:
    ddp_rank = 0
    ddp_local_rank = 0
    ddp_world_size = 1
    master_process = True
#model = GPT.from_pretrained('gpt2')
    device = "cpu"
    if torch.cuda.is_available():
        device = "cuda"
    print(f'using device: {device}')

torch.manual_seed(1337)
if torch.cuda.is_available():
    torch.cuda.manual_seed(1337)


total_bs = 524288 # 2 ** 19
B = 32
T = 1024
assert total_bs % (B * T * ddp_world_size) == 0, f"total batch size: {total_bs} should be divided by (B*T) * (ddp_world_size): {B}*{T}*{ddp_world_size}."
grad_accum_steps = total_bs // (B * T * ddp_world_size)
if master_process:
    print(f"total batch size: {total_bs}.")
    print(f"compute the corresponding gradient accumulation steps: {grad_accum_steps}")
#print(f"I am GPU {ddp_local_rank}.")
#import sys;sys.exit(0)

train_loader = DataLoaderLite(B=B,T=T,process_rank=ddp_rank,num_processes=ddp_world_size,split="train")
val_loader = DataLoaderLite(B=B,T=T,process_rank=ddp_rank,num_processes=ddp_world_size,split="val")
torch.set_float32_matmul_precision('high')
model = GPT(GPTConfig(vocab_size=50304))
model.to(device)
model = torch.compile(model)

if ddp:
    DDP(model,device_ids=[ddp_local_rank])
raw_model = model#.module if ddp else model

max_lr = 6e-4
min_lr = 0.1 * max_lr
warm_up_steps = 715
max_steps = 19073
def get_lr(step):
    if step < warm_up_steps:
        return (step + 1) * max_lr / warm_up_steps
    if step > max_steps:
        return min_lr
    decay_ratio = (step - warm_up_steps) / (max_steps - warm_up_steps)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))
    return min_lr + coeff * (max_lr - min_lr)

#logits, loss = model(x.to(device),y.to(device))
#optimizer = torch.optim.AdamW(model.parameters(),lr=6e-4,betas=(0.9,0.95),eps=1e-8)
optimizer = raw_model.configure_optimizers(weight_decay=0.1,lr=6e-4,device=device)
for step in range(max_steps):
    t0 = time.time()
    if step % 100 == 0:
        model.eval()
        val_loader.reset()
        with torch.no_grad():
            val_loss_accum = 0.0
            val_loss_steps = 20
            for _ in range(val_loss_steps):
                x,y = val_loader.next_batch()
                x,y = x.to(device),y.to(device)
                with torch.autocast(device_type=device,dtype=torch.bfloat16):
                    logits,loss = model(x,y)
                loss = loss / val_loss_steps
                val_loss_accum += loss.detach()
        if ddp:
            dist.all_reduce(val_loss_accum,op=dist.ReduceOp.AVG)
        if master_process:
            print(f"validation loss: {val_loss_accum.item():.4f}")

    if step > 0 and step % 100 == 0:
        model.eval()
        num_return_seqs = 4
        max_length = 32
        tokens = enc.encode("Hello, I'm a language model,")
        tokens = torch.tensor(tokens,dtype=torch.long)
        tokens = tokens.unsqueeze(0).repeat(num_return_seqs,1)
        xgen = tokens.to(device)
        sample_rng = torch.Generator(device=device)
        sample_rng.manual_seed(42 + ddp_rank)
        while xgen.size(1) < max_length:
            with torch.no_grad():
                logits,loss = model(xgen)
                logits = logits[:,-1,:] # B,T,vocab_size, take the last token
                probs = F.softmax(logits,dim=-1)
                topk_probs,topk_indices = torch.topk(probs,50,dim=-1)
                ix = torch.multinomial(topk_probs,1)    #(B,1)
                xcol = torch.gather(topk_indices,-1,ix) #(B,1)
                xgen = torch.cat((xgen,xcol),dim=1)
        for i in range(num_return_seqs):
            tokens = x[i,:max_length].tolist()
            decoded = enc.decode(tokens)
            print(f"rank {ddp_rank}, sample {i}: {decoded}")


    model.train()
    optimizer.zero_grad()
    loss_accum = 0.0
    for micro_step in range(grad_accum_steps):
        x,y = train_loader.next_batch()
        x,y = x.to(device),y.to(device)
        with torch.autocast(device_type=device,dtype=torch.bfloat16):
            logits,loss = model(x,y)
            #import code; code.interact(local=locals())
            loss = loss / grad_accum_steps
            loss_accum += loss.detach()
            if ddp:
                model.require_backward_grad_sync = (micro_step == grad_accum_steps - 1)
            loss.backward()
    if ddp:
        dist.all_reduce(loss_accum,op=dist.ReduceOp.AVG)
    norm = torch.nn.utils.clip_grad_norm_(model.parameters(),max_norm=1.0)
    lr = get_lr(step)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    optimizer.step()
    torch.cuda.synchronize()
    t1 = time.time()
    dt = (t1 - t0) * 1000
    tokens_per_sec = (train_loader.B * train_loader.T * grad_accum_steps * ddp_world_size) / (t1 - t0)
    if master_process:
        print(f"step {step} | lr: {lr} | loss: {loss_accum.item()} | norm: {norm:.4f} |  dt: {dt:.2f}ms | tok/sec: {tokens_per_sec:.1f}")
if ddp:
    destroy_process_group()
