"""Directory identity and employee membership are different questions (q015)."""
from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor, ExecCompleted, ExecFailed
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

E='dbpcm_warehouse.employee'
D='dbpcm_warehouse.department'


def rows(columns, values):
    return {'columns':columns,'rows':values,'row_count':len(values),'truncated':False}


def setup(scripted):
    detail=BlueprintDetail(id='bp-directory',intent='Employees in a department',slots_summary='department',
        uses=frozenset({E+'.employee_code',E+'.department_name',D+'.department_name'}),
        status='validated',drift_status='clean',hit_count=0,catalog_sha='',
        slots=[{'name':'department','type':'entity','required':True,'binds_to':D+'.department_name'}],
        sql_template=f'SELECT employee_code FROM {E} WHERE department_name = {{department}}',
        result_grain=['employee_code'])
    index=FakeVectorIndex();index.add_detail(detail)
    catalog=CatalogHandle({E:{'employee_code':'String','department_name':'String'},D:{'department_name':'String'}})
    mcp=FakeMCPClient(scripted={'runQuery':scripted})
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp,catalog),vector_index=index),mcp


async def test_directory_match_with_zero_employees_completes():
    executor,mcp=setup([rows(['department_name'],[['Information Technology']]),rows(['employee_code'],[]),rows(['__bp_n','__bp_d'],[[0,0]])])
    result=await executor.execute(blueprint_id='bp-directory',slot_bindings={'department':'Information Technology'},credentials=RuntimeCredentials(session_id='s',jwt='test',column_scope=frozenset()))
    assert isinstance(result,ExecCompleted)
    assert result.result_full['row_count']==0
    assert D in mcp.calls[0].args['sql']
    assert E in mcp.calls[1].args['sql']


async def test_abbreviation_miss_returns_to_agent_without_guessing_or_running_node():
    executor,mcp=setup([rows(['department_name'],[])])
    result=await executor.execute(blueprint_id='bp-directory',slot_bindings={'department':'IT'},credentials=RuntimeCredentials(session_id='s',jwt='test',column_scope=frozenset()))
    assert isinstance(result,ExecFailed)
    assert result.error_code=='BLUEPRINT_ENTITY_NOT_FOUND'
    assert result.retryable
    assert len(mcp.calls)==1
