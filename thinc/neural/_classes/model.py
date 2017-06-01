from __future__ import division, unicode_literals
from numpy import prod
import numpy
import contextlib
import msgpack
import msgpack_numpy

msgpack_numpy.patch()

from .. import util
from ..train import Trainer
from ..ops import NumpyOps
from ..mem import Memory
from ..util import get_ops, copy_array, normalize_string_keys
from ... import check
from ... import describe
from ...check import equal_length, has_shape, is_sequence, is_float, is_array


class Model(object):
    '''Model base class.'''
    name = 'model'
    id = 0
    lsuv = False
    ops = NumpyOps()
    Ops = NumpyOps
    Trainer = Trainer
    drop_factor = 1.0
    descriptions = []
    on_data_hooks = []
    on_init_hooks = [] # Use this to add layers
    _operators = {}

    @classmethod
    @contextlib.contextmanager
    def define_operators(cls, operators):
        '''Bind operators to specified functions for the scope of the context:

        Example
        -------

            model = Model()
            other = Model()
            with Model.define_operators({"+": lambda self, other: "plus"}):
                print(model + other)
                # "plus"
            print(model + other)
            # Raises TypeError --- binding limited to scope of with block.
        '''
        old_ops = dict(cls._operators)
        for op, func in operators.items():
            cls._operators[op] = func
        yield
        cls._operators = old_ops

    @classmethod
    @contextlib.contextmanager
    def use_device(cls, device):
        '''Change the device to execute on for the scope of the block.'''
        if device == cls.ops.device:
            yield
        else:
            curr_Ops, curr_ops = (cls.Ops, cls.ops)
            cls.Ops = get_ops(device)
            cls.ops = cls.Ops()
            yield
            cls.Ops = curr_Ops
            cls.ops = curr_ops

    @property
    def input_shape(self):
        raise NotImplementedError

    @property
    def output_shape(self):
        raise NotImplementedError

    def __init__(self, *args, **kwargs):
        self.name = self.__class__.name
        self.ops = self.Ops()
        kwargs = self._update_defaults(args, kwargs)
        self._mem = Memory(self.ops)
        self._dims = {}
        if not hasattr(self, '_layers'):
            self._layers = []
        self.descriptions = dict(self.descriptions)
        self.on_init_hooks = list(self.on_init_hooks)
        self.on_data_hooks = list(self.on_data_hooks)

        for attr, install in self.descriptions.items():
            install(attr, self)
        for hook in self.on_init_hooks:
            hook(self, *args, **kwargs)
        self.set_id()

    def _update_defaults(self, args, kwargs):
        new_kwargs = {}
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                new_kwargs[key] = value
        return new_kwargs

    def set_id(self):
        Model.id += 1
        self.id = Model.id
        for layer in self._layers:
            layer.set_id()

    #@check.args(equal_length)
    @check.arg(1, is_sequence)
    def begin_training(self, train_X, train_y=None, **trainer_cfg):
        for hook in self.on_data_hooks:
            hook(self, train_X, train_y)
        return self.Trainer(self, **trainer_cfg)

    @check.arg(2, is_float)
    @check.arg(1, has_shape(('nB', 'nI')))
    def begin_update(self, X, drop=0.0):
        raise NotImplementedError

    def predict(self, X):
        y, _ = self.begin_update(X)
        return y

    def predict_one(self, x):
        X = self.ops.expand_dims(x, axis=0)
        return self.predict(X)[0]

    @contextlib.contextmanager
    def use_params(self, params): # pragma: no cover
        backup = None
        weights = self._mem.weights
        if self.id in params:
            param = params[self.id]
            backup = weights.copy()
            copy_array(weights, param)
        if hasattr(self, '_layers'):
            contexts = [layer.use_params(params) for layer in self._layers]
            for context in contexts:
                next(context.gen)
        yield
        if backup is not None:
            copy_array(self._mem.weights, backup)
        for i, context in enumerate(contexts):
            # This is ridiculous, but apparently it's what you
            # have to do to make this work across Python 2/3?
            try:
                next(context.gen)
            except StopIteration:
                pass

    def __call__(self, x):
        '''
        x
            Must match expected type
            Must match expected shape
        '''
        return self.predict(x)

    def pipe(self, stream, batch_size=1000):
        for batch in util.minibatch(stream, batch_size):
            ys = self.predict(batch)
            for y in ys:
                yield y

    def update(self, stream, batch_size=1000):
        for X, y in util.minibatch(stream, batch_size=batch_size):
            output, finish_update = self.begin_update(X)
            gradient = finish_update(y)
            yield gradient

    def evaluate(self, X, y):
        '''
        x
            Must match expected type
            Must match expected shape
        y
            Must match expected type
        '''
        scores = self.ops.xp.vstack(self.pipe(X))
        scores = scores.reshape(y.shape)
        if len(scores.shape) == 1:
            correct = ((scores >= 0.5) == (y >= 0.5)).sum()
        else:
            correct = (scores.argmax(axis=1) == y.argmax(axis=1)).sum()
        return correct / y.shape[0]

    def evaluate_logloss(self, X, y, minimum=None, maximum=None):
        yh = self.ops.xp.vstack(self.pipe(X))
        yh = yh.reshape(y.shape)
        if minimum is not None:
            yh = self.ops.xp.maximum(yh, minimum)
        if maximum is not None:
            yh = self.ops.xp.minimum(yh, maximum)
        assert len(yh.shape) == 1
        losses = -y * self.ops.xp.log(yh + 1e-8) - (1-y) * self.ops.xp.log((1-yh)+1e-8)
        return losses.mean()

    @check.operator_is_defined('+')
    def __add__(self, other):
        '''Apply the function bound to the '+' operator.'''
        return self._operators['+'](self, other)

    @check.operator_is_defined('-')
    def __sub__(self, other):
        '''Apply the function bound to the '-' operator.'''
        return self._operators['-'](self, other)

    @check.operator_is_defined('*')
    def __mul__(self, other):
        '''Apply the function bound to the '*' operator.'''
        return self._operators['*'](self, other)

    @check.operator_is_defined('@')
    def __matmul__(self, other):
        '''Apply the function bound to the '@' operator.'''
        return self._operators['@'](self, other)

    @check.operator_is_defined('/')
    def __div__(self, other):
        '''Apply the function bound to the '/' operator.'''
        return self._operators['/'](self, other)

    @check.operator_is_defined('/')
    def __truediv__(self, other): # pragma: no cover
        '''Apply the function bound to the '/' operator.'''
        return self._operators['/'](self, other)

    @check.operator_is_defined('//')
    def __floordiv__(self, other):
        '''Apply the function bound to the '//' operator.'''
        return self._operators['//'](self, other)

    @check.operator_is_defined('%')
    def __mod__(self, other):
        '''Apply the function bound to the '%' operator.'''
        return self._operators['%'](self, other)

    @check.operator_is_defined('**')
    def __pow__(self, other, modulo=None):
        '''Apply the function bound to the '**' operator.'''
        return self._operators['**'](self, other)

    @check.operator_is_defined('<<')
    def __lshift__(self, other):
        '''Apply the function bound to the '<<' operator.'''
        return self._operators['<<'](self, other)

    @check.operator_is_defined('>>')
    def __rshift__(self, other):
        '''Apply the function bound to the '>>' operator.'''
        return self._operators['>>'](self, other)

    @check.operator_is_defined('&')
    def __and__(self, other):
        '''Apply the function bound to the '&' operator.'''
        return self._operators['&'](self, other)

    @check.operator_is_defined('^')
    def __xor__(self, other):
        '''Apply the function bound to the '^' operator.'''
        return self._operators['^'](self, other)

    @check.operator_is_defined('|')
    def __or__(self, other):
        '''Apply the function bound to the '|' operator.'''
        return self._operators['|'](self, other)

    # This stuff really belongs in thinc -- but I expect
    # to refactor how all this works in thinc anyway.
    # What a mess!
    def to_bytes(self):
        weights = []
        queue = [self]
        i = 0
        for layer in queue:
            if hasattr(layer, '_mem'):
                weights.append({
                    u'dims': normalize_string_keys(getattr(layer, '_dims', {})),
                    u'params': []})
                if hasattr(layer, 'seed'):
                    weights[-1][u'seed'] = layer.seed

                for (id_, name), (start, row, shape) in layer._mem._offsets.items():
                    if row == 1:
                        continue
                    param = layer._mem.get((id_, name))
                    if not isinstance(layer._mem.weights, numpy.ndarray):
                        param = param.get()
                    weights[-1][u'params'].append(
                        {
                            u'name': name,
                            u'offset': start,
                            u'shape': shape,
                            u'value': param,
                        }
                    )
                i += 1
            if hasattr(layer, '_layers'):
                queue.extend(layer._layers)
        return msgpack.dumps({u'weights': weights},
                use_bin_type=True, encoding='utf8')

    def from_bytes(self, bytes_data):
        data = msgpack.loads(bytes_data, encoding='utf8')
        weights = data[u'weights']
        queue = [self]
        i = 0
        for layer in queue:
            if hasattr(layer, '_mem'):
                if u'seed' in weights[i]:
                    layer.seed = weights[i][u'seed']
                for dim, value in weights[i][u'dims'].items():
                    setattr(layer, dim, value)
                for param in weights[i][u'params']:
                    name = param[u'name']
                    if isinstance(name, bytes):
                        name = name.decode('utf8')
                    dest = getattr(layer, name)
                    copy_array(dest, param[b'value'])
                i += 1
            if hasattr(layer, '_layers'):
                queue.extend(layer._layers)
