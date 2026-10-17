"""Replay one frozen proposal, optionally with its actually learned skill."""
import argparse
import json
import os
from pathlib import Path
from arena import validate_spec
from runtime import dump, run_target


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("proposal",type=Path)
    parser.add_argument("--environment",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--learned-skill",type=Path)
    parser.add_argument("--control",action="store_true")
    parser.add_argument("--repeats",type=int,default=2)
    parser.add_argument("--seed",type=int)
    parser.add_argument("--env-file",type=Path,default=Path('/Users/caiyuheng/Documents/ChatGPT/hackson/nemotron-redteam/.env'))
    args=parser.parse_args()
    if not 1<=args.repeats<=3:parser.error('repeats must be 1..3')
    for line in args.env_file.read_text().splitlines():
        if line.startswith('CRUSOE_API_KEY='):os.environ['CRUSOE_API_KEY']=line.split('=',1)[1].strip()
    spec=json.loads(args.proposal.read_text())['spec']
    if args.seed is not None:spec['seed']=args.seed
    spec=validate_spec(spec)
    record=json.loads(args.environment.read_text())['model']
    skill=args.learned_skill.read_text() if args.learned_skill else None
    args.out.mkdir(parents=True,exist_ok=False)
    results=[]
    for i in range(args.repeats):
        print('REPLAY',i,flush=True)
        result=run_target(spec,not args.control,args.out/f'replay-{i}',record,learned_skill=skill)
        results.append(result);dump(args.out/'results.json',results)
        print(json.dumps({k:result[k] for k in ('passed','violations','declared_status','native_multiagent_observed','timed_out')}),flush=True)
