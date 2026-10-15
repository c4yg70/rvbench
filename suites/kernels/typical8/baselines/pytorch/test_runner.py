"""Run on the Linux PyTorch worker: python -m unittest test_runner -v."""
import unittest
import importlib

import torch
import runner


class BaselineContracts(unittest.TestCase):
    def input(self):
        return {'name':'x','kind':'tensor','shape':[3,4],'dtype':'float16',
                'layout':{'kind':'contiguous'},
                'generator':{'kind':'torch_cpu_uniform_v1','seed':7,'low':-1,'high':1}}

    def test_materialized_dtype_seed_and_content_are_preserved(self):
        item = self.input()
        first, second = runner.materialize(torch,item), runner.materialize(torch,item)
        self.assertEqual(first.dtype,torch.float16)
        self.assertEqual(runner.tensor_hash(first),runner.tensor_hash(second))
        item['generator']['seed'] += 1
        self.assertNotEqual(runner.tensor_hash(first),runner.tensor_hash(runner.materialize(torch,item)))

    def test_strided_inputs_preserve_declared_layout(self):
        item = self.input(); item['layout']={'kind':'strided','strides':[9,2],'storage_offset':3}
        actual = runner.materialize(torch,item)
        self.assertEqual(actual.stride(),(9,2)); self.assertEqual(actual.storage_offset(),3)
        self.assertEqual(list(actual.shape),[3,4])

    def test_unknown_generator_never_falls_back(self):
        item = self.input(); item['generator']['kind']='not-supported'
        with self.assertRaisesRegex(ValueError,'generator'): runner.materialize(torch,item)

    def test_partial_attention_cannot_pass_full_interface(self):
        expected={'outputs':[{'name':'out','kind':'tensor','shape':[2,3],'dtype':'float16'}]}
        status,reason,checks=runner.verify(torch,{'id':'_flash_attention_forward'},expected,{},[torch.zeros(2,3,dtype=torch.float16)],True)
        self.assertEqual(status,'unverifiable'); self.assertEqual(reason['code'],'SDPA_OUT_ONLY')

    def test_call_signature_is_pinned(self):
        op={'id':'abs','call':{'implementations':{'reference':{'kind':'python_symbol','id':'torch.neg'}}}}
        with self.assertRaisesRegex(ValueError,'binding'):
            runner.build_call(op,{'attrs':{}},{},importlib.import_module('abs').baseline,'torch.abs')

    def test_each_operator_owns_an_importable_native_baseline(self):
        for name in ['abs','aminmax','softmax','mv','mm','conv2d','topk','flash_attention_forward']:
            module=importlib.import_module(name)
            self.assertTrue(callable(module.baseline),name)

    def test_per_operator_function_is_the_callable_executed(self):
        seen=[]
        def baseline(x):
            seen.append(x)
            return 'native-result'
        op={'call':{'args':[{'kind':'input','name':'x'}],'kwargs':{},
                    'implementations':{'reference':{'kind':'python_symbol','id':'torch.abs'}}}}
        call,_=runner.build_call(op,{'attrs':{}},{'x':'tensor'},baseline,'torch.abs')
        self.assertEqual(call(),'native-result')
        self.assertEqual(seen,['tensor'])


if __name__ == '__main__':
    unittest.main()
