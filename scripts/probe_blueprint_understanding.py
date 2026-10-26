"""Opt-in Fireworks probe: real prompt/tools and getBlueprint projection; no SQL executes."""
import argparse, asyncio, copy, json, time
from pathlib import Path
import httpx, yaml
from dotenv import dotenv_values
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT
from data_agent.runtime.mcp.tool_schema import (RUN_BLUEPRINT_TOOL_SCHEMA, GET_BLUEPRINT_TOOL_SCHEMA,
    SEARCH_BLUEPRINTS_TOOL_SCHEMA, ASK_USER_TOOL_SCHEMA, FINALIZE_ANSWER_SCHEMA, RECORD_ASSUMPTIONS_TOOL_SCHEMA)
from data_agent.runtime.retrieval.tools import _enrich_slot, _composition_annotation


def scenarios(root):
    def load(name): return yaml.safe_load((root/'app/corpus/data/blueprints'/f'{name}.yaml').read_text())
    base=load('bp-average-salary-by-department')
    conflict=copy.deepcopy(base);conflict['id']='probe-active-only'
    conflict['slots']=[slot for slot in conflict['slots'] if slot['name']!='employee_status']
    conflict['sql_template']=conflict['sql_template'].replace("(employee_status = {employee_status} OR {employee_status} = 'All')", "employee_status = 'Active'")
    conflict['slots_summary']='department';conflict['uses_rules']=[];conflict['intent']='Average compensation by department and pay type for Active employees.'
    return [
      ('default_population','Average compensation by department.',base),
      ('all_statuses','Average compensation by department across all employee statuses, including terminated employees.',base),
      ('explicit_active','Average compensation by department for Active employees only.',base),
      ('period_range','Show employee hires by month between January 1, 2025 inclusive and January 1, 2026 exclusive?',load('bp-hires-in-range')),
      ('composed','Which departments exceed the corresponding company average compensation for each pay type?',load('bp-departments-above-company-average-salary')),
      ('incompatible_blueprint','Average compensation by department.',conflict),
      ('compatible_explicit_active','Average compensation by department for Active employees only.',conflict),
      ('repair_population','Average compensation by department. The earlier Active-only result was rejected: use Active and On Leave. Correct that population without repeating the unchanged query.',conflict),
      ('slot_explanation','Before running this blueprint, explain what omitting employee_status does, how to include all statuses, and whether I should supply SQL or raw slot values.',base),
    ]


def projection(bp):
    out={k:bp[k] for k in ('id','intent','slots_summary','uses','status','drift_status','catalog_sha','sql_template','uses_rules','result_grain') if k in bp}
    out.update(found=True,slots=[_enrich_slot(s) for s in bp.get('slots',[])])
    if bp.get('composes'):out['composition']=_composition_annotation(bp['composes'])
    return out

async def main(args):
    env=dotenv_values(args.env_file)
    key=next(v for k,v in env.items() if 'FIREWORK' in k and 'KEY' in k and v)
    root=Path(args.backend);employee=yaml.safe_load((root/'app/semantic_catalog/data/employee.yaml').read_text())
    schemas=[RUN_BLUEPRINT_TOOL_SCHEMA,GET_BLUEPRINT_TOOL_SCHEMA,SEARCH_BLUEPRINTS_TOOL_SCHEMA,ASK_USER_TOOL_SCHEMA,FINALIZE_ANSWER_SCHEMA,RECORD_ASSUMPTIONS_TOOL_SCHEMA]
    if args.runquery_schema:
        schemas.append(json.loads(Path(args.runquery_schema).read_text()))
    tools=[{'type':'function','function':s} for s in schemas]
    semaphore=asyncio.Semaphore(3)
    async with httpx.AsyncClient(timeout=150) as client:
      async def one(case):
       name,question,bp=case
       messages=[{'role':'system','content':AGENT_SYSTEM_PROMPT},
         {'role':'system','content':'Current date: 2026-10-25. Scoped catalog documentation: '+json.dumps({'dbpcm_warehouse.employee':employee})},
         {'role':'user','content':question},
         {'role':'assistant','content':None,'tool_calls':[{'id':'probe_get','type':'function','function':{'name':'getBlueprint','arguments':json.dumps({'id':bp['id']})}}]},
         {'role':'tool','tool_call_id':'probe_get','content':json.dumps(projection(bp))}]
       start=time.monotonic()
       async with semaphore:
        response=await client.post('https://api.fireworks.ai/inference/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={'model':args.model,'messages':messages,'tools':tools,'temperature':0,'max_tokens':args.max_tokens})
       response.raise_for_status();payload=response.json()
       payload['choices'][0]['message'].pop('reasoning_content',None)
       return {'case':name,'question':question,'messages':messages,'tools':tools,'response':payload['choices'][0], 'usage':payload.get('usage'),'wall_seconds':round(time.monotonic()-start,2)}
      results=await asyncio.gather(*(one(c) for c in scenarios(root)))
    Path(args.output).write_text(json.dumps({'model':args.model,'max_tokens':args.max_tokens,'scope':'Synthetic next-action comprehension probe, not a full agent or database execution. Real agent system prompt, local tool schemas, enriched getBlueprint slots, catalog employee documentation. No production user data.','results':results},indent=2))
    for r in results:print(r['case'],json.dumps(r['response']['message'],ensure_ascii=False))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--env-file',required=True);p.add_argument('--backend',required=True);p.add_argument('--output',required=True);p.add_argument('--max-tokens',type=int,default=8000);p.add_argument('--runquery-schema');p.add_argument('--model',default='accounts/fireworks/models/deepseek-v4p1-flash');asyncio.run(main(p.parse_args()))
