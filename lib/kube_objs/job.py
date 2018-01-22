# (c) Copyright 2017-2018 OLX

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from kube_obj import KubeObj
from kube_types import *
from .environment import *
from .pod import *
from .selectors import *
from .mixins import EnvironmentPreProcessMixin

class Job(KubeObj):
    apiVersion = 'batch/v1'
    kind = 'Job'
    kubectltype = 'job'

    _defaults = {
        'activeDeadlineSeconds': 100,
        'backoffLimit': 5,
        'parallelism': 1,
        'completions': 1,
        'pod_template': PodTemplateSpec(restartPolicy='Never'),
        }

    _types = {
        'activeDeadlineSeconds': Positive(NonZero(Integer)),
        'backoffLimit': Positive(NonZero(Integer)),
        'parallelism': Positive(NonZero(Integer)),
        'completions': Positive(NonZero(Integer)),
        'pod_template': PodTemplateSpec
        }

    _parse_default_base = ('spec',)

    _parse = {
        'pod_template': ('spec', 'template'),
        }

    _exclude = {
        '.status': True,
        }

    def render(self):
        ret = self.renderer(mapping={'pod_template': 'template'})
        del ret['name']
        return {'metadata': {'name': self._data['name']}, 'spec': ret}
