# (c) Copyright 2017-2018 OLX

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import fnmatch
import json
import os
import sys
import weakref

import loader

import kube_yaml
from load_python_core import do_compile_internal
from kube_obj import KubeObj, KubeBaseObj
from obj_registry import obj_registry
from user_error import UserError, user_originated
from output import RubiksOutputError, OutputCollection
from lookup import Resolver
from util import mkdir_p

import kube_objs
import kube_vartypes


class PythonStopCompile(Exception):
    pass


class PythonFileCollection(loader.Loader):
    _python_file_types = None

    @classmethod
    def get_python_file_type(cls, extension=None):
        def _rec_subclasses(kls):
            ret = {}
            for c in kls.__subclasses__():
                ret.update(_rec_subclasses(c))
            if hasattr(kls, 'extensions'):
                for e in kls.extensions:
                    ret[e] = kls
            return ret

        if cls._python_file_types is None:
            cls._python_file_types = _rec_subclasses(PythonBaseFile)

        if extension is None:
            ret = {}
            ret.update(cls._python_file_types)
            return ret

        try:
            return cls._python_file_types[extension]
        except KeyError:
            return None

    def __init__(self, repository, content_check=None):
        loader.Loader.__init__(self, repository)
        self.outputs = OutputCollection(self, repository, content_check=content_check)
        self.all_sources = None
        self.current_context = []

    def get_file_context(self, path):
        try:
            self.current_context.append(path)
            if path.extension is None:
                raise UserError(loader.LoaderFileNameError(
                    "Filenames must have an extension in {}".format(path.full_path)))

            python_loader = self.__class__.get_python_file_type(path.extension)

            if python_loader is None:
                raise UserError(loader.LoaderFileNameError(
                    "No valid handler for extension {} in {}".format(path.extension, path.full_path)))

            return self.get_or_add_file(path, python_loader, (self, path))
        finally:
            assert self.current_context[-1] is path
            self.current_context.pop()

    def find_all_source_files(self):
        if self.all_sources is None:
            basepath = self.repository.sources
            paths = []
            def _rec_add(path):
                d_ents = os.listdir(os.path.join(self.repository.basepath, basepath, path, '.'))
                for d_ent in sorted(d_ents):
                    if d_ent.startswith('.'):
                        continue
                    if os.path.exists(os.path.join(self.repository.basepath, basepath, path, d_ent, '.')):
                        if path == '.':
                            _rec_add(d_ent)
                        else:
                            _rec_add(os.path.join(path, d_ent))
                        continue
                    paths.append(loader.Path(os.path.join(self.repository.basepath,
                                                          basepath, path, d_ent), self.repository))
            _rec_add('.')
            self.all_sources = paths

        return self.all_sources

    def load_all_python(self, basepath):
        extensions = self.__class__.get_python_file_type(None)
        good_ext = set()
        for ext in extensions:
            if extensions[ext].default_export_objects:
                good_ext.add(ext)

        paths = self.find_all_source_files()
        for p in paths:
            if os.path.relpath(p.repo_rel_path, basepath).startswith('..'):
                continue
            if p.extension is None:
                continue
            if p.extension not in good_ext:
                continue
            self.load_python(p)

    def load_python(self, path):
        if isinstance(path, loader.Path):
            pth = path
        else:
            pth = loader.Path(os.path.join(self.repository.basepath, path), self.repository)
        self.debug(1, 'loading python {}'.format(pth.repo_rel_path))

        self.current_file = pth
        self.get_file_context(pth)
        self.current_file = None

    def get_multi_python(self, py_context, pattern='*', basepath=None, **kwargs):
        extensions = self.__class__.get_python_file_type(None)
        ret = {}

        if pattern is None:
            raise UserError(ValueError("get_multi_python(): pattern must be specified"))

        basep = None
        if basepath is not None:
            try:
                basep = py_context.path.rel_path(basepath)
            except AssertionError as e:
                raise UserError(LoaderImportError('basepath specifiers in get_multi_python must be relative: {}'.
                                                  format(basepath)))

        for p in self.find_all_source_files():
            if basep is not None and os.path.relpath(p.repo_rel_path, basep.repo_rel_path).startswith('..'):
                continue
            if p.extension is None:
                continue
            if p.extension not in extensions:
                continue
            if not fnmatch.fnmatchcase(p.filename, pattern) and \
                    not fnmatch.fnmatchcase(p.basename, pattern):
                continue

            self.add_dep(self.current_context[-1], p)

            try:
                ret[p.repo_rel_path] = self.get_file_context(p).get_module(**kwargs)
            except loader.LoaderBaseException as e:
                raise UserError(e)
        return ret

    def import_python(self, py_context, name, exports, **kwargs):
        path = self.import_check(py_context, name)

        self.debug(1, 'importing python {} ({} -> {})'.format(path.repo_rel_path, py_context.path.repo_rel_path, name))

        basename = path.basename
        if 'import_as' in kwargs and kwargs['import_as'] is not None:
            basename = kwargs['import_as']

        self.add_dep(self.current_context[-1], path)

        new_context = None
        try:
            new_context = self.get_file_context(path)

            if 'no_import' not in kwargs or not kwargs['no_import']:
                if 'no_import' in kwargs:
                    del kwargs['no_import']
                if 'import_as' in kwargs:
                    del kwargs['import_as']

                self.import_symbols(name, new_context.path, py_context.path, basename,
                                    new_context, py_context._current_module, exports, **kwargs)

            elif len(exports) != 0 or ('import_as' in kwargs and kwargs['import_as'] is not None):
                raise UserError(ValueError("import_python: Can't set symbols to import to when using no_import"))

        except loader.LoaderBaseException as e:
            raise UserError(e)

        if new_context is not None:
            return new_context.get_module(**kwargs)
        return new_context

    def add_output(self, kobj):
        self.outputs.add_output(kobj)

    def gen_output(self):
        return self.outputs.write_output()


class PythonBaseFile(object):
    _kube_objs = None
    _kube_vartypes = None
    compile_in_init = True
    default_export_objects = False
    can_cluster_context = True

    @classmethod
    def get_kube_objs(cls):
        if cls._kube_objs is None:
            cls._kube_objs = {}

            for k in kube_objs.__dict__:
                if isinstance(kube_objs.__dict__[k], type) and k not in ('KubeObj', 'KubeBaseObj', 'KubeSubObj'):
                    try:
                        if isinstance(kube_objs.__dict__[k](), KubeBaseObj):
                            cls._kube_objs[k] = kube_objs.__dict__[k]
                    except:
                        pass

        return cls._kube_objs

    @classmethod
    def get_kube_vartypes(cls):
        if cls._kube_vartypes is None:
            cls._kube_vartypes = {}

            for k in kube_vartypes.__dict__:
                if isinstance(kube_vartypes.__dict__[k], type) and k not in ('VarEntity'):
                    try:
                        if isinstance(kube_vartypes.__dict__[k](_test=True), kube_vartypes.var_types.VarEntity):
                            cls._kube_vartypes[k] = kube_vartypes.__dict__[k]
                    except:
                        pass
        return cls._kube_vartypes

    def __init__(self, collection, path):
        if path.basename == '' or path.basename.lower().strip('0123456789abcdefghijklmnopqrstuvwxyz_') != '':
            raise UserError(ValueError(
                "Filenames should be python compliant (alphanumeric and '_'), found: {}".format(path.basename)))

        if hasattr(self, 'extensions') and len(self.extensions) != 0:
            assert path.extension in self.extensions

        self.path = path
        self.collection = weakref.ref(collection)

        self.output_was_called = False
        self.default_import_args = {}

        self.import_exception = None

        if self.compile_in_init:
            save_cluster = KubeBaseObj._default_cluster
            try:
                KubeBaseObj._default_cluster = None
                self.module = self.do_compile()
            finally:
                KubeBaseObj._default_cluster = save_cluster

    def warn_import_exception(self, cluster=None):
        if self.import_exception is not None:
            exc = self.import_exception[1]
            if exc.__class__ is UserError:
                exc = exc.exc

            if cluster is None:
                print("Warning: swallowed import exception in {} to import {}: {}: {}".format(
                    self.path.src_rel_path, self.import_exception[0],
                    exc.__class__.__name__,
                    str(exc)), file=sys.stderr,
                    )
            else:
                print("Warning: swallowed import exception in {} ({}) to import {}: {}: {}".format(
                    self.path.src_rel_path, cluster, self.import_exception[0],
                    exc.__class__.__name__,
                    str(exc)), file=sys.stderr,
                    )

    def debug(self, *args):
        return self.collection().debug(*args)

    def get_module(self, **kwargs):
        return self.module

    def get_symnames(self, **kwargs):
        return self.module.__dict__.keys()

    def get_symbol(self, symname, **kwargs):
        return self.module.__dict__[symname]

    def default_ns(self):
        def _user_error(fn):
            def internal_call(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except PythonStopCompile:
                    raise
                except Exception as e:
                    raise UserError(e)
            return internal_call

        def import_python(name, *exports, **kwargs):
            self.debug(3, '{}: import_python({}, ...)'.format(self.path.src_rel_path, name))

            nargs = {}
            nargs.update(self.default_import_args)
            nargs.update(kwargs)
            nargs['__reserved_names'] = self.reserved_names

            try:
                return self.collection().import_python(self, name, exports, **nargs)
            except Exception as e:
                if self.import_exception is None:
                    self.import_exception = (name, e)
                raise

        def get_multi_python(pattern=None, basepath=None, **kwargs):
            nargs = {}
            nargs.update(self.default_import_args)
            nargs.update(kwargs)

            return self.collection().get_multi_python(self, pattern=pattern, basepath=basepath, **nargs)

        def output(val):
            self.output_was_called = True
            return self.collection().add_output(val)

        def no_output():
            self.output_was_called = True

        def yaml_dump(obj):
            return kube_yaml.yaml_safe_dump(obj, default_flow_style=False)

        @_user_error
        def yaml_load(string):
            return kube_yaml.yaml_load(string)

        def stop():
            raise PythonStopCompile("stop")

        def json_dump(obj, expanded=True):
            if expanded:
                args = {'indent': 2}
            else:
                args = {'indent': None, 'separators': (',',':')}

            try:
                return json.dumps(obj, **args)
            except TypeError:
                ret = kube_vartypes.JSON(obj)
                ret.args = args
                return ret

        @_user_error
        def json_load(string):
            return json.loads(string)

        @_user_error
        def get_lookup(*path, **kwargs):
            tmp = list()
            for p in path:
              tmp.append(self.path.rel_path(p))

            return Resolver(tmp, **kwargs)

        @_user_error
        def read_file(path, cant_read_ok=False):
            path = self.path.rel_path(path)
            try:
                with open(path.full_path) as f:
                    return f.read()
            except:
                if cant_read_ok:
                    return None
                raise

        def load_object(obj):
            return KubeObj.parse_obj(obj)

        def run_command(*cmd, **kwargs):
            args = {'cwd': None, 'env_clear': False, 'env': None, 'delay': True, 'ignore_rc': True,
                    'rstrip': True, 'eol': False}
            for k in kwargs:
                if k not in args:
                    raise UserError(TypeError("{} isn't a valid argument to run_command()".format(k)))
            args.update(kwargs)

            cwd = None
            if args['cwd'] is not None:
                cwd = self.path.rel_path(args['cwd'])

            good_rc = None
            if not args['ignore_rc']:
                good_rc = (0,)
            cmd_ent = kube_vartypes.Command(cmd, cwd=cwd, env_clear=args['env_clear'],
                                            env=args['env'], good_rc=good_rc, rstrip=args['rstrip'], eol=args['eol'])
            if args['delay']:
                return cmd_ent
            else:
                return str(cmd_ent)

        def fileinfo():
            return {
                'current_file_full_path': self.path.full_path,
                'current_file_repo_path': self.path.repo_rel_path,
                'current_file_full_dir': self.path.full_dir,
                'current_file_repo_dir': self.path.repo_rel_dir,
                'load_file_full_path': self.collection().current_file.full_path,
                'load_file_repo_path': self.collection().current_file.repo_rel_path,
                'load_file_full_dir': self.collection().current_file.full_dir,
                'load_file_repo_dir': self.collection().current_file.repo_rel_dir,
                }

        def cluster_context(clstr):
            class cluster_wrapper(object):
                def __init__(self, clstr):
                    self.cluster = clstr

                def __enter__(self):
                    self.save_cluster = KubeBaseObj._default_cluster
                    KubeBaseObj._default_cluster = self.cluster

                def __exit__(self, etyp, evalue, etb):
                    KubeBaseObj._default_cluster = self.save_cluster
                    return False

            return cluster_wrapper(self.collection().repository.get_cluster_info(clstr))

        def namespace(ns):
            class namespace_wrapper(object):
                def __init__(self, ns):
                    self.ns = ns

                def __enter__(self):
                    self.save_ns = KubeBaseObj._default_ns
                    KubeBaseObj._default_ns = self.ns

                def __exit__(self, etyp, evalue, etb):
                    KubeBaseObj._default_ns = self.save_ns
                    return False

            return namespace_wrapper(ns)

        def current_namespace():
            return KubeBaseObj._default_ns

        clusters = tuple(self.collection().repository.get_clusters())

        def cluster_info(c):
            assert c in clusters
            return self.collection().repository.get_cluster_info(c)

        ret = {
            'repobase': self.collection().repository.basepath,

            'import_python': import_python,
            'get_multi_python': get_multi_python,
            'namespace': namespace,
            'current_namespace': current_namespace,

            'stop': stop,

            'yaml_load': yaml_load,
            'json_load': json_load,
            'yaml_dump': yaml_dump,
            'json_dump': json_dump,

            'read_file': read_file,
            'run_command': run_command,
            'get_lookup': get_lookup,

            'fileinfo': fileinfo,

            'load_object': load_object,

            'output': output,
            'no_output': no_output,
            }

        if len(clusters) != 0:
            ret['clusters'] = clusters
            ret['cluster_info'] = cluster_info

            if self.can_cluster_context:
                ret['cluster_context'] = cluster_context

        ret.update(self.__class__.get_kube_objs())
        ret.update(self.__class__.get_kube_vartypes())

        self.reserved_names = tuple(ret.keys())

        return ret

    def do_compile(self, extra_context=None):
        self.debug(2, 'compiling python: {} ({})'.format(self.path.src_rel_path, self.path.full_path))
        mod = None
        savepath = sys.path
        try:
            newpath = []
            if hasattr(self.collection().repository, 'pythonpath'):
                newpath.extend(self.collection().repository.pythonpath)
            newpath.extend(sys.path)
            sys.path = newpath
            self.debug(3, 'sys.path = {}'.format(':'.join(sys.path)))

            with open(self.path.full_path) as f:
                src = f.read()

            ctx = self.default_ns()
            if extra_context is not None:
                ctx.update(extra_context)

            obj_registry().new_context(id(self))
            finished_ok = False
            try:
                o_exc = None
                try:
                    mod = do_compile_internal(
                        self, src,
                        os.path.relpath(self.path.full_path),
                        self.path.dot_path(), self.path.full_path, ctx, (PythonStopCompile,))
                except UserError as e:
                    o_exc = sys.exc_info()
                    e.prepend_tb(o_exc[2])
                    raise
                except SyntaxError:
                    raise
                except Exception:
                    o_exc = sys.exc_info()
                    if not user_originated(o_exc[2]):
                        raise

                if extra_context is None or 'current_cluster_name' not in extra_context:
                    self.warn_import_exception()
                else:
                    self.warn_import_exception(extra_context['current_cluster_name'])

                if o_exc is not None:
                    raise UserError(o_exc[1], tb=o_exc[2])

                finished_ok = True
            finally:
                objs = obj_registry().close_context(id(self))
                if finished_ok and not self.output_was_called and self.default_export_objects:
                    for o in objs:
                        if isinstance(o, KubeObj) and o._data[o.identifier] is not None:
                            try:
                                self.collection().add_output(o)
                            except UserError as e:
                                e.f_file = o._caller_file
                                e.f_line = o._caller_line
                                e.f_fn = o._caller_fn
                                raise e
        except SyntaxError:
            raise
        except UserError:
            raise
        except Exception as e:
            if loader.DEV:
                raise
            raise loader.LoaderCompileException('Got exception while loading/compiling {}: {}: {}'.format(
                                                self.path.src_rel_path, e.__class__.__name__, str(e)))
        finally:
            sys.path = savepath

        return mod


class PythonImportFile(PythonBaseFile):
    extensions = ('kube',)


class PythonRunOnceFile(PythonImportFile):
    default_export_objects = True
    extensions = ('gkube',)


class PythonImportPerClusterFile(PythonBaseFile):
    compile_in_init = False
    default_export_objects = False
    extensions = ('ckube',)
    can_cluster_context = False

    def __init__(self, *args, **kwargs):
        PythonBaseFile.__init__(self, *args, **kwargs)
        clusters = self.collection().repository.get_clusters()

        if len(clusters) == 0:
            self.fallback = True
            save_cluster = KubeBaseObj._default_cluster
            res_save_cluster = Resolver.current_cluster

            def valid_clusters(*clusters):
                print("WARN: valid_clusters() called in clusterless mode", file=sys.stderr)

            try:
                KubeBaseObj._default_cluster = None
                Resolver.current_cluster = None
                self.module = self.do_compile({
                    'current_cluster': None,
                    'current_cluster_name': None,
                    'valid_clusters': valid_clusters,
                    })
            finally:
                KubeBaseObj._default_cluster = save_cluster
                Resolver.current_cluster = res_save_cluster
        else:
            self.fallback = False
            self.module = {}
            for c in self.collection().repository.get_clusters():
                this_cluster = self.collection().repository.get_cluster_info(c)
                save_cluster = KubeBaseObj._default_cluster
                res_save_cluster = Resolver.current_cluster

                def get_valid_clusters(c):
                    def valid_clusters(*clusters):
                        if len(clusters) == 0:
                            raise UserError(ValueError("Must specify at least one cluster"))
                        all_clusters = tuple(self.collection().repository.get_clusters())
                        for cc in clusters:
                            if cc not in all_clusters:
                                print("WARN: valid_clusters() called with unknown cluster name: " + cc,
                                      file=sys.stderr)
                        if c not in clusters:
                            raise PythonStopCompile("stop")
                    return valid_clusters

                try:
                    KubeBaseObj._default_cluster = this_cluster
                    Resolver.current_cluster = this_cluster
                    self.default_import_args = {'cluster': c}
                    self.module[c] = self.do_compile({
                        'current_cluster': this_cluster,
                        'current_cluster_name': c,
                        'valid_clusters': get_valid_clusters(c),
                        })
                finally:
                    KubeBaseObj._default_cluster = save_cluster
                    Resolver.current_cluster = res_save_cluster

    def get_module(self, **kwargs):
        if self.fallback:
            return self.module
        if not 'cluster' in kwargs:
            raise loader.LoaderImportError("must specify 'cluster' param when importing .ekube or .ckube files")
        return self.module[kwargs['cluster']]

    def get_symnames(self, **kwargs):
        if self.fallback:
            return self.module.__dict__.keys()
        if not 'cluster' in kwargs:
            raise loader.LoaderImportError("must specify 'cluster' param when importing .ekube or .ckube files")
        return self.module[kwargs['cluster']].__dict__.keys()

    def get_symbol(self, symname, **kwargs):
        if self.fallback:
            return self.module.__dict__[symname]
        if not 'cluster' in kwargs:
            raise loader.LoaderImportError("must specify 'cluster' param when importing .ekube or .ckube files")
        return self.module[kwargs['cluster']].__dict__[symname]


class PythonRunPerClusterFile(PythonImportPerClusterFile):
    default_export_objects = True
    extensions = ('ekube',)
