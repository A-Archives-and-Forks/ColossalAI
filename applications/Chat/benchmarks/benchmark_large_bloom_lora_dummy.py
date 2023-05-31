import argparse
from copy import deepcopy

import torch
import torch.distributed as dist
import torch.nn as nn
from coati.models.base import RewardModel
from coati.models.bloom import BLOOMActor, BLOOMCritic
from coati.trainer import PPOTrainer
from coati.trainer.callbacks import PerformanceEvaluator
from coati.trainer.strategies import ColossalAIStrategy, Strategy
from torch.optim import Adam
from torch.utils.data import DataLoader
from transformers import AutoTokenizer
from transformers.modeling_utils import no_init_weights
from transformers.models.bloom.configuration_bloom import BloomConfig

from colossalai.nn.optimizer import HybridAdam


def get_model_numel(model: nn.Module, strategy: Strategy) -> int:
    numel = sum(p.numel() for p in model.parameters())
    if isinstance(strategy, ColossalAIStrategy) and strategy.stage == 3 and strategy.shard_init:
        numel *= dist.get_world_size()
    return numel


def preprocess_batch(samples) -> dict:
    input_ids = torch.stack(samples)
    attention_mask = torch.ones_like(input_ids, dtype=torch.long)
    return {'input_ids': input_ids, 'attention_mask': attention_mask}


def print_rank_0(*args, **kwargs) -> None:
    if dist.get_rank() == 0:
        print(*args, **kwargs)


def print_model_numel(model_dict: dict) -> None:
    B = 1024**3
    M = 1024**2
    K = 1024
    outputs = ''
    for name, numel in model_dict.items():
        outputs += f'{name}: '
        if numel >= B:
            outputs += f'{numel / B:.2f} B\n'
        elif numel >= M:
            outputs += f'{numel / M:.2f} M\n'
        elif numel >= K:
            outputs += f'{numel / K:.2f} K\n'
        else:
            outputs += f'{numel}\n'
    print_rank_0(outputs)


def get_gpt_config(model_name: str) -> BloomConfig:
    model_map = {
        '125m': BloomConfig.from_pretrained('facebook/opt-125m'),
        '350m': BloomConfig(hidden_size=1024, ffn_dim=4096, num_hidden_layers=24, num_attention_heads=16),
        '700m': BloomConfig(hidden_size=1280, ffn_dim=5120, num_hidden_layers=36, num_attention_heads=20),
        '1.3b': BloomConfig.from_pretrained('facebook/opt-1.3b'),
        '2.7b': BloomConfig.from_pretrained('facebook/opt-2.7b'),
        '3.5b': BloomConfig(hidden_size=3072, ffn_dim=12288, num_hidden_layers=32, num_attention_heads=32),
        '5.5b': BloomConfig(hidden_size=3840, ffn_dim=15360, num_hidden_layers=32, num_attention_heads=32),
        '6.7b': BloomConfig.from_pretrained('facebook/opt-6.7b'),
        '10b': BloomConfig(hidden_size=5120, ffn_dim=20480, num_hidden_layers=32, num_attention_heads=32),
        '13b': BloomConfig.from_pretrained('facebook/opt-13b'),
    }
    try:
        return model_map[model_name]
    except KeyError:
        raise ValueError(f'Unknown model "{model_name}"')


def main(args):
    if args.strategy == 'colossalai_gemini':
        strategy = ColossalAIStrategy(stage=3, placement_policy='cuda', initial_scale=2**5)
    elif args.strategy == 'colossalai_gemini_cpu':
        strategy = ColossalAIStrategy(stage=3, placement_policy='cpu', initial_scale=2**5)
    elif args.strategy == 'colossalai_gemini_reshard':
        strategy = ColossalAIStrategy(stage=3, placement_policy='cuda_reshard', initial_scale=2**5)
    else:
        raise ValueError(f'Unsupported strategy "{args.strategy}"')

    torch.cuda.set_per_process_memory_fraction(args.cuda_mem_frac)

    model_config = get_gpt_config(args.model)
    critic_config = get_gpt_config(args.critic_model)
    with strategy.model_init_context(), no_init_weights():
        actor = BLOOMActor(config=model_config, lora_rank=args.lora_rank, checkpoint=args.grad_checkpoint)
        actor.model.tie_weights()
        critic = BLOOMCritic(config=critic_config, lora_rank=args.lora_rank, checkpoint=args.grad_checkpoint)
        critic.model.tie_weights()

        initial_model = BLOOMActor(config=model_config, lora_rank=args.lora_rank, checkpoint=args.grad_checkpoint)
        reward_model = BLOOMCritic(config=critic_config, lora_rank=args.lora_rank, checkpoint=args.grad_checkpoint)
        reward_model = RewardModel(reward_model.model, reward_model.value_head)

    if args.use_kernels:
        from coati.kernels import convert_to_xformer_model
        actor, critic, initial_model, reward_model = map(convert_to_xformer_model,
                                                         (actor, critic, initial_model, reward_model))

    actor_numel = get_model_numel(actor, strategy)
    critic_numel = get_model_numel(critic, strategy)
    initial_model_numel = get_model_numel(initial_model, strategy)
    reward_model_numel = get_model_numel(reward_model, strategy)
    print_model_numel({
        'Actor': actor_numel,
        'Critic': critic_numel,
        'Initial model': initial_model_numel,
        'Reward model': reward_model_numel
    })
    performance_evaluator = PerformanceEvaluator(actor_numel,
                                                 critic_numel,
                                                 initial_model_numel,
                                                 reward_model_numel,
                                                 enable_grad_checkpoint=False,
                                                 ignore_episodes=1)

    if args.strategy.startswith('colossalai'):
        actor_optim = HybridAdam(actor.parameters(), lr=5e-6)
        critic_optim = HybridAdam(critic.parameters(), lr=5e-6)
    else:
        actor_optim = Adam(actor.parameters(), lr=5e-6)
        critic_optim = Adam(critic.parameters(), lr=5e-6)

    tokenizer = AutoTokenizer.from_pretrained('facebook/opt-350m')
    tokenizer.pad_token = tokenizer.eos_token

    (actor, actor_optim), (critic, critic_optim), initial_model, reward_model = strategy.prepare(
        (actor, actor_optim), (critic, critic_optim), initial_model, reward_model)

    # TODO(ver217): load checkpoint here

    trainer = PPOTrainer(strategy,
                         actor,
                         critic,
                         reward_model,
                         initial_model,
                         actor_optim,
                         critic_optim,
                         ptx_coef=0,
                         max_epochs=args.max_epochs,
                         train_batch_size=args.train_batch_size,
                         offload_inference_models=args.offload_inference_models,
                         max_length=512,
                         do_sample=True,
                         temperature=1.0,
                         top_k=50,
                         use_cache=True,
                         pad_token_id=tokenizer.pad_token_id,
                         eos_token_id=tokenizer.eos_token_id,
                         callbacks=[performance_evaluator])

    random_prompts = torch.randint(tokenizer.vocab_size, (1000, 256), device=torch.cuda.current_device())
    dataloader = DataLoader(random_prompts,
                            batch_size=args.experience_batch_size,
                            shuffle=True,
                            collate_fn=preprocess_batch)

    trainer.fit(dataloader,
                None,
                num_episodes=args.num_episodes,
                max_timesteps=args.max_timesteps,
                update_timesteps=args.update_timesteps)

    print_rank_0(f'Peak CUDA mem: {torch.cuda.max_memory_allocated()/1024**3:.2f} GB')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='125m')
    parser.add_argument('--critic_model', default='125m')
    parser.add_argument('--strategy',
                        choices=[
                            'colossalai_gemini',
                            'colossalai_gemini_reshard',
                            'colossalai_gemini_cpu',
                        ],
                        default='colossalai_gemini_reshard')
    parser.add_argument('--num_episodes', type=int, default=3)
    parser.add_argument('--max_timesteps', type=int, default=1)
    parser.add_argument('--update_timesteps', type=int, default=1)
    parser.add_argument('--max_epochs', type=int, default=1)
    parser.add_argument('--train_batch_size', type=int, default=8)
    parser.add_argument('--experience_batch_size', type=int, default=8)
    parser.add_argument('--lora_rank', type=int, default=0)
    parser.add_argument('--cuda_mem_frac', type=float, default=1.0)
    parser.add_argument('--offload_inference_models', action='store_true', default=False)
    parser.add_argument('--use_kernels',
                        action='store_true',
                        default=False,
                        help='This uses xformers kernels, which can save memory and accelerate training.')
    parser.add_argument('--grad_checkpoint',
                        default=False,
                        action='store_true',
                        help='This uses gradient checkpointing, which can save memory and slow down training.')
    args = parser.parse_args()
    main(args)
