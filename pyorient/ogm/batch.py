from .broker import get_broker
from .commands import Command, VertexCommand, CreateEdgeCommand, RetrievalCommand, create_cache_callback

from .vertex import VertexVector
from .what import What, LetVariable, VertexWhatMixin, EdgeWhatMixin

from .expressions import ExpressionMixin
from .query_utils import ArgConverter

import re
import string
from copy import copy

class Batch(ExpressionMixin):
    READ_COMMITTED = 0
    REPEATABLE_READ = 1

    def __init__(self, graph, isolation_level=READ_COMMITTED, cache=None, compile=False):
        self.graph = graph
        self.objects = {}
        self.variables = {}
        self.stack = [[]]
        self.cacher = create_cache_callback(graph, cache)
        self.cache = cache
        self.compile = compile

        if isolation_level == Batch.REPEATABLE_READ:
            self.stack[0].append('BEGIN ISOLATION REPEATABLE_READ')
        else:
            self.stack[0].append('BEGIN')

        for name,cls in graph.registry.items():
            broker = get_broker(cls)
            if broker:
                self.objects[cls] = broker = BatchBroker(broker)
            else:
                self.objects[cls] = broker = BatchBroker(cls.objects)

            broker_name = getattr(cls, 'registry_plural', None)
            if broker_name is not None:
                setattr(self, broker_name, broker)

    def __setitem__(self, key, value):
        """Add a command to the batch.
        :param key: A name for the variable storing the results of the command,
        or an empty slice if command is only meant for its side-effects.
        Names can be reused.
        :param value: The command to perform.
        """
        if isinstance(key, slice):
            command = str(value)
            self.stack[-1].append('{}'.format(command))
        else:
            VarType = BatchVariable
            if isinstance(value, Command):
                command = str(value)

                if isinstance(value, VertexCommand):
                    VarType = BatchVertexVariable
                elif isinstance(value, CreateEdgeCommand):
                    VarType = BatchEdgeVariable
                elif isinstance(value, RetrievalCommand):
                    VarType = BatchQueryVariable
            else:
                if isinstance(value, BatchVariable):
                    VarType = value.__class__
                command = ArgConverter.convert_to(ArgConverter.Vertex, value, self)

            key = Batch.clean_name(key) if Batch.clean_name else key

            self.stack[-1].append('LET {} = {}'.format(key, command))

            self.variables[key] = VarType('${}'.format(key), value)

    def sleep(self, ms):
        """Put the batch in wait.
        :param ms: Number of milliseconds.
        """
        self.stack[-1].append('sleep {}'.format(ms))

    def clear(self):
        """Clear the batch for a new set of commands."""
        # TODO Give option to reuse batches?
        self.variables.clear()

        # Stack size should be 1
        self.stack[0] = self.stack[0][:1]

    def if_(self, condition):
        """Conditional execution in a batch.
        :param condition: Anything that can be passed to Query.filter()
        """
        return BatchBranch(self, condition)

    def __str__(self):
        return u'\n'.join(self.stack[-1])

    def __getitem__(self, key):
        """Commit batch with return value, or reference a previously defined
        variable.

        Using a plain string as a key commits and returns the named variable.

        Slicing with only a 'stop' value does not commit - it is the syntax for
        using a variable. Otherwise slicing can give finer control over commits;
        step values give a retry limit, and a start value denotes the returned
        variable.

        When called within a BatchCompiler block, returns a CompiledBatch instance.
        """

        def copy_variable(name):
            if Batch.clean_name:
                return copy(self.variables[Batch.clean_name(name)])
            elif any(c in Batch.INVALID_CHARS for c in name) or name[0].isdigit():
                raise ValueError(
                    'Variable name \'{}\' contains invalid character(s).'
                        .format(name))

            return copy(self.variables[name])

        returned = None
        if isinstance(key, slice):
            if key.step:
                if key.start:
                    returned = Batch.return_string(key.start)
                    self.stack[-1].append(
                        'COMMIT RETRY {}\nRETURN {}'.format(key.step, returned))
                else:
                    self.stack[-1].append('COMMIT RETRY {}'.format(key.step))
            elif key.stop:
                # No commit.
                return copy_variable(key.stop)
            else:
                if key.start:
                    returned = Batch.return_string(key.start)
                    self.stack[-1].append('COMMIT\nRETURN {}'.format(returned))
                else:
                    self.stack[-1].append('COMMIT')
        else:
            returned = Batch.return_string(key)
            self.stack[-1].append('COMMIT\nRETURN {}'.format(returned))

        def memoized_query_response(variables):
            variable = variables.get(returned[1:], None)
            class memodict(dict):
                def __missing__(self, key):
                    val = self[key] = isinstance(variable, BatchQueryVariable) 
                    return val
            check = memodict().__getitem__
            return lambda response: len(response) > 1 or check('bqv')
        # Before variables are cleared...
        is_query_response = memoized_query_response(self.variables) 

        if self.compile:
            command_source = CompiledBatch(str(self))
            finalise_batch = lambda executor: command_source.set_executor(executor)
        else:
            command_source = str(self)
            finalise_batch = lambda executor: executor()

        self.clear()
        g = self.graph

        def make_batch_executor(returned, caching):
            if caching:
                getter = lambda: g.client.batch(str(command_source), None, None, self.cacher)
            else:
                getter = lambda: g.client.batch(str(command_source))

            if returned:
                if returned[0] in ('[', '{'):
                    processor = lambda response: \
                        g.elements_from_records(response, self.cache) if response else None
                else:
                    def processor(response):
                        if response:
                            if is_query_response(response):
                                return g.elements_from_records(response, self.cache)
                            else:
                                return g.element_from_record(response[0], self.cache)
                def handler():
                    return processor(getter())
                return handler
            else:
                def handler():
                    getter()
                return handler

        return finalise_batch(make_batch_executor(returned, self.cacher is not None))

    def collect(self, *variables, **kwargs):
        """Commit batch, collecting batch variables in a dict.

        :param variables: Names of variables to collect.
        :param kwargs: 'retries', a limit for retries in event of
        concurrent modifications. 'fetch': A fetch plan, for all collected
        queries.

        When called within a BatchCompiler block, returns a CompiledBatch instance.
        """

        # Until OrientDB supports multiple expand()s in a single query, we are
        # confined to creative use of unionall().
        # Like pascal strings, prefix each query's result set with a
        # run-length.
        # e.g., for results from variables a, b, c:
        # [3, a1, a2, a3, 2, b1, b2, 1, c1]

        rle = True # TODO Once OrientDB supports multiple expand()s

        # Ignore duplicates
        variables = set(variables)
        if rle:
            for var in variables:
                self.stack[-1].append('LET _{0} = SELECT ${0}.size() as size'.format(var))

        retries = kwargs.get('retries', None)
        if retries is not None:
            self.stack[-1].append('COMMIT RETRY {}'.format(retries))
        else:
            self.stack[-1].append('COMMIT')

        fetch = kwargs.get('fetch', '')
        if fetch:
            fetch = ' FETCHPLAN ' + fetch

        if rle:
            self.stack[-1].append(
                'RETURN (SELECT expand(unionall({})){})'.format(
                ','.join(['$_{0},${0}'.format(var) for var in variables]),
                fetch))

        if self.compile:
            command_source = CompiledBatch(str(self))
            finalise_batch = lambda executor: command_source.set_executor(executor)
        else:
            command_source = str(self)
            finalise_batch = lambda executor: executor()

        self.clear()
        g = self.graph

        if self.cacher:
            getter = lambda: g.client.batch(str(command_source), None, None, self.cacher)
        else:
            getter = lambda: g.client.batch(str(command_source))

        def collect():
            response = getter()

            collected = {}
            if rle:
                run_idx = 1
                for var in variables:
                    run_length = response[run_idx-1].oRecordData['size']

                    sentinel = run_idx + run_length
                    collected[var] = g.elements_from_records(response[run_idx:sentinel], self.cache)
                    run_idx = sentinel + 1
            return collected

        return finalise_batch(collect)

    def commit(self, retries=None):
        """Commit batch with no return value.

        When called within a BatchCompiler block, returns a CompiledBatch instance.
        """
        self.stack[-1].append('COMMIT' + (' RETRY {}'.format(retries) if retries else ''))

        g = self.graph

        if self.compile:
            command_source = CompiledBatch(str(self))
            finalise_batch = lambda executor: command_source.set_executor(executor, suppress_return=True)
        else:
            command_source = str(self)
            finalise_batch = lambda executor: executor() and None

        self.clear()
        if self.cacher:
            execute_batch = lambda: g.client.batch(str(command_source), None, None, self.cacher)
        else:
            execute_batch = lambda: g.client.batch(str(command_source))

        return finalise_batch(execute_batch)

    @staticmethod
    def return_string(variables):
        cleaned = Batch.clean_name or (lambda s:s)

        from pyorient.ogm.query import Query
        # Since any value can be returned from a batch,
        # '$' must be used when a variable is referenced
        if isinstance(variables, str):
            if variables[0] == '$':
                return '{}'.format('$' + cleaned(variables[1:]))
            else:
                return repr(variables)
        elif isinstance(variables, Query):
            return '({})'.format(variables)
        elif isinstance(variables, (list, tuple)):
            return '[' + ','.join(
                '${}'.format(cleaned(var)) for var in variables) + ']'
        elif isinstance(variables, dict):
            return '{' + ','.join(
                '{}:${}'.format(repr(k),cleaned(v))
                    for k,v in variables.items()) + '}'
        else:
            return '{}'.format(variables)

    INVALID_CHARS = frozenset(''.join(c for c in string.punctuation if c is not '_') + string.whitespace)

    @staticmethod
    def default_name_cleaner(name):
        # Can't begin with a digit
        rx = r'^\d|[' + re.escape(''.join(Batch.INVALID_CHARS)) + r']'
        return re.sub(rx, '_', name)

    clean_name = None
    @classmethod
    def use_name_cleaner(cls, cleaner=default_name_cleaner):
        cls.clean_name = cleaner

class BatchBranch():
    IF = 'if ({}) {{\n  {}\n}}'
    FORMAT_IF = 'if ({}) {{{{\n  {}\n}}}}'
    def __init__(self, batch, condition):
        self.batch = batch
        self.condition = condition

    def __enter__(self):
        self.batch.stack.append([])

    def __exit__(self, e_type, e_value, e_trace):
        batch = self.batch

        if e_type is not None:
            # If an exception was raised, abort the batch
            batch.stack[-1].append('ROLLBACK')
        branch_commands = '\n'.join(batch.stack.pop())

        if_str = BatchBranch.FORMAT_IF if batch.compile else BatchBranch.IF

        batch.stack[-1].append(
            if_str.format(
                    ArgConverter.convert_to(ArgConverter.Boolean, self.condition, batch),
                    branch_commands
                ))

        # Suppress exceptions from batch
        return True

class RollbackException(Exception):
    pass

class BatchCompiler(object):
    """Suppress execution of batch to allow for later formatting."""
    def __init__(self, batch):
        self.batch = batch

    def __enter__(self):
        self.batch.compile = True

    def __exit__(self, e_type, e_value, e_trace):
        self.batch.compile = False
        # Don't suppress exceptions during compile
        return False

class CompiledBatch(RetrievalCommand):
    def __init__(self, compiled, executor=None):
        self._compiled = compiled
        self._formatted = None
        self._executor = executor

    def __str__(self):
        """A simplified version of format()"""
        if not self._formatted:
            self._formatted = self._compiled.format()
        return self._formatted

    def set_executor(self, executor, suppress_return=False):
        if suppress_return:
            def generator():
                while True:
                    yield executor() and None
        else:
            def generator():
                while True:
                    yield executor()

        self._executor = generator()
        return self

    def format(self, *args, **kwargs):
        """Format the compiled batch string, replacing tokens akin to str.replace()
        :return: The formatted string, after storing it for use by execute()
        """
        if self._formatted is None or args or kwargs:
            encode = self.FORMAT_ENCODER
            self._formatted = self._compiled.format(*[encode(arg) for arg in args], **{k:encode(v) for k,v in kwargs.items()})

        return self._formatted

    def execute(self):
        """Execute the compiled batch"""
        try:
            return next(self._executor)
        except StopIteration:
            return None

    """Alias for execute()"""
    __call__ = execute

class BatchBroker(object):
    def __init__(self, broker):
        self.broker = broker

    def __getattribute__(self, name):
        suffix = '_command'
        if name == 'broker':
            return super(BatchBroker, self).__getattribute__(name)
        elif name.endswith(suffix):
            return self.broker.__getattribute__(name)
        else:
            return self.broker.__getattribute__(name + suffix)

class BatchVariable(LetVariable):
    def __init__(self, reference, value):
        super(BatchVariable, self).__init__(reference[1:])
        self._id = reference
        self._value = value

    def __copy__(self):
        return type(self)(self._id, self._value)

class BatchVertexVariable(BatchVariable, VertexWhatMixin):
    def __init__(self, reference, value):
        super(BatchVertexVariable, self).__init__(reference, value)

    def __call__(self, edge_or_broker):
        if hasattr(edge_or_broker, 'broker'):
            edge_or_broker = edge_or_broker.broker.element_cls
        elif hasattr(edge_or_broker, 'element_cls'):
            edge_or_broker = edge_or_broker.element_cls

        if edge_or_broker.decl_type == 1:
            return BatchVertexVector(self, edge_or_broker.objects)

class BatchEdgeVariable(BatchVariable, EdgeWhatMixin):
    def __init__(self, reference, value):
        super(BatchEdgeVariable, self).__init__(reference, value)

class BatchQueryVariable(BatchVariable):
    pass

class BatchVertexVector(VertexVector):
    def __init__(self, origin, edge_broker, **kwargs):
        super(BatchVertexVector, self).__init__(origin, edge_broker, **kwargs)

    def __gt__(self, target):
        """Syntactic sugar for creating an edge in a batch."""
        return self.edge_broker.create_command(
            self.origin, target, **self.kwargs)

    def __lt__(self, origin):
        """Syntactic sugar for creating an edge in a batch.

        Convenient when 'origin' vertex defined outside batch.
        """
        if hasattr(origin, '_id'):
            return self.edge_broker.create_command(
                origin
                , self.origin # Target
                , **self.kwargs)
        return self


