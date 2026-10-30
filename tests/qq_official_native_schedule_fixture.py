
from tests.qq_official_file_fixture import ExecutionFixture
from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.turn_input import load_turn_input
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.adapters.qq_official.native_schedules import NativeSchedules,NativeCommandSource,digest
from src.plugins.ai_chat.adapters.qq_official.native_schedule_policy import DeploymentSchedulePolicy,NativeScheduleConfiguration


class NativeFixture(ExecutionFixture):
    def setUp(self):
        super().setUp()
        self.deliveries=DeliveryStore(self.database,recover_interrupted=False);self.addCleanup(self.deliveries.close)
        self.usage=UsageStore(self.database);self.addCleanup(self.usage.close)
        self.outbox=OfficialSendStore(self.deliveries,admission=self.admission,pepper=b'TEST_NATIVE_ONLY'*3,
            clock=lambda:self.now,allow_newer_windows=True)
        with self.tx() as c:
            observe_ready(c,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_SESSION','user':{'id':'TEST_REAL_BOT','bot':True}}},self.now)
        self.handle=self.claim(kind='command').handle
        with self.tx() as c:
            execution.begin_command_execution(c,**self.args(self.handle))
            inbound=load_turn_input(c,execution_api=execution,**self.args(self.handle))
        self.source=NativeCommandSource(self.handle,self.job,inbound)
        self.policy=DeploymentSchedulePolicy(NativeScheduleConfiguration(True,'TEST_COST_POLICY',1,self.now+365*86400))
        self.calls=0
        async def model(work):self.calls+=1;return MessageBody((TextNode(0,'scheduled result'),))
        self.engine=NativeSchedules(database=self.database,token=self.token,jobs=self.jobs,usage=self.usage,
            outbox=self.outbox,admission=self.admission,policy=self.policy,clock=lambda:self.now,run_model=model)
        self.outbox.source_guard=self.engine.plan_guard

    def reminder(self,key='r',delay=10):
        return self.engine.create_reminder(self.source,request_key=digest(key),text='remember TEST',scheduled_for=self.now+delay)

    def routine(self,key='routine',**kwargs):
        args=dict(request_key=digest(key),name='daily TEST',prompt='summarize TEST',cron='* * * * *',timezone='UTC')
        args.update(kwargs)
        return self.engine.set_routine(self.source,**args)

    def claim_native(self):
        return self.jobs.claim_due('TEST_NATIVE_WORKER',kinds=self.engine.kinds,now=self.now)[0]

    def finish_foreground(self):
        self.result(self.handle,text='created')
        self.assertTrue(self.jobs.mark_succeeded(self.job.job_id,self.job.lease_owner,now=self.now,lease_generation=self.job.lease_generation))
