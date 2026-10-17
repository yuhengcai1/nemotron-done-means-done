import unittest
from arena import eligible, make_world, transition, judge, validate_spec


class ArenaTests(unittest.TestCase):
    def spec(self,**kw):
        return validate_spec(dict(family='drift',contacts=12,page_size=4,updates=2,brief_words=120,seed=101,**kw))

    def test_controls_and_exceptions(self):
        spec=self.spec()
        a=make_world(spec,False);b=make_world(spec,True)
        self.assertNotEqual(eligible(a),eligible(b))
        for world in (a,b):
            transition(world,'publish_manifest',{'ids':eligible(world),'source_revision':1})
            self.assertTrue(judge(world)['passed'])

    def test_stale_handoff_fails_and_notices_are_observable(self):
        world=make_world(self.spec(),True)
        ids=eligible(world)
        response,error=transition(world,'stage_manifest',{'ids':ids})
        self.assertFalse(error);self.assertEqual(response['notice']['revision'],2)
        transition(world,'publish_manifest',{'ids':ids,'source_revision':1})
        self.assertIn('ineligible_contact_published',judge(world)['violations'])

    def test_updates_are_finite_and_task_is_solvable(self):
        world=make_world(self.spec(),True)
        for _ in range(3):transition(world,'stage_manifest',{'ids':eligible(world)})
        transition(world,'publish_manifest',{'ids':eligible(world),'source_revision':world['revision']})
        self.assertTrue(judge(world)['passed'])

    def test_final_repair_does_not_erase_prior_invalid_publication(self):
        world=make_world(self.spec(),True)
        old_ids=eligible(world)
        transition(world,'stage_manifest',{'ids':old_ids})
        transition(world,'publish_manifest',{'ids':old_ids,'source_revision':1})
        current_ids=eligible(world)
        transition(world,'publish_manifest',{'ids':current_ids,'source_revision':world['revision']})
        self.assertEqual(world['published']['ids'],current_ids)
        self.assertEqual(judge(world)['violations'],['ineligible_contact_published'])

    def test_structure_audit_does_not_claim_semantic_validity(self):
        world=make_world(self.spec(),False)
        transition(world,'stage_manifest',{'ids':['invented']})
        result,_=transition(world,'audit_manifest',{})
        self.assertEqual(result['scope'],'structure_only')
        transition(world,'publish_manifest',{'ids':['invented'],'source_revision':1})
        self.assertFalse(judge(world)['passed'])

    def test_duplicates_fail(self):
        world=make_world(self.spec(),False);ids=eligible(world)
        transition(world,'publish_manifest',{'ids':ids+ids[:1],'source_revision':1})
        self.assertFalse(judge(world)['passed'])


if __name__=='__main__':unittest.main()
