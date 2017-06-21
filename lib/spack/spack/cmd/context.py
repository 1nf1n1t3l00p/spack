import llnl.util.tty as tty
import spack
import llnl.util.filesystem as fs
import spack.util.spack_json as sjson
import spack.util.spack_yaml as syaml
from spack.spec import Spec, CompilerSpec, FlagMap
from spack.repository import Repo
from spack.version import VersionList

import argparse
import itertools
import os
import shutil

_db_dirname = fs.join_path(spack.var_path, 'contexts')

class Context(object):
    def __init__(self, name):
        self.name = name
        self.user_specs = list()
        self.concretized_order = list()
        self.specs_by_hash = dict()
        # Libs in this set must always appear as the dependency traced from any
        # root of link deps
        self.common_libs = dict() # name -> hash
        # Packages in this set must always appear as the dependency traced from
        # any root of run deps
        self.common_bins = dict() # name -> hash

    def add(self, user_spec):
        query_spec = Spec(user_spec)
        existing = set(x for x in self.user_specs
                       if Spec(x).name == query_spec.name)
        if existing:
            tty.die("Package {0} was already added to {1}"
                    .format(query_spec.name, self.name))
        self.user_specs.append(user_spec)

    def remove(self, query_spec):
        query_spec = Spec(query_spec)
        match_index = -1
        for i, spec in enumerate(self.user_specs):
            if Spec(spec).name == query_spec.name:
                match_index = i
                break

        if match_index < 0:
            tty.die("Not found: {0}".format(query_spec))

        del self.user_specs[match_index]
        if match_index < len(self.concretized_order):
            spec_hash = self.concretized_order[match_index]
            del self.concretized_order[match_index]
            del self.specs_by_hash[spec_hash]

    def concretize(self):
        num_concretized = len(self.concretized_order)
        new_specs = list()
        for user_spec in self.user_specs[num_concretized:]:
            spec = Spec(user_spec)
            spec.concretize()
            new_specs.append(spec)
            self.specs_by_hash[spec.dag_hash()] = spec
            self.concretized_order.append(spec.dag_hash())
        return new_specs

    def install(self):
        for concretized_hash in self.concretized_order:
            spec = self.specs_by_hash[concretized_hash]
            spec.package.do_install(explicit=True)

    def list(self, stream, include_deps=False):
        for user_spec, concretized_hash in itertools.izip_longest(
            self.user_specs, self.concretized_order):

            stream.write('{0}\n'.format(user_spec))

            if concretized_hash:
                concretized_spec = self.specs_by_hash[concretized_hash]
                if include_deps:
                    stream.write(concretized_spec.tree())
                else:
                    stream.write(concretized_spec.format() + '\n')

    def upgrade_dependency(self, dep_name, dry_run=False):
        """
        Note: if you have
        
        w -> x -> y
        
        and
        
        v -> x -> y
        
        Then if you upgrade y, you will start by re-concretizing w (and x).
        This should make sure that v uses the same x as w if this context is
        supposed to reuse dependencies where possible. The difference compared
        to 'normal' concretization is that you want to keep things as similar
        as possible. I think the approach would be to go through all the
        common_libs and common_bins, recognize the first time they get
        re-concretized, and then replace them manually where encountered later.
        """
        new_order = list()
        new_deps = list()
        for i, spec_hash in enumerate(self.concretized_order):
            spec = self.specs_by_hash[spec_hash]
            if dep_name in spec:
                if dry_run:
                    tty.msg("Would upgrade {0} for {1}"
                            .format(spec[dep_name].format(), spec.format()))
                else:
                    new_spec = upgrade_dependency_version(spec, dep_name)
                    new_order.append(new_spec.dag_hash())
                    self.specs_by_hash[new_spec.dag_hash()] = new_spec
                    new_deps.append(new_spec[dep_name])
            else:
                new_order.append(spec_hash)

        if not dry_run:
            self.concretized_order = new_order
            return new_deps[0] if new_deps else None

    def reset_os_and_compiler(self, compiler=None):
        new_order = list()
        new_specs_by_hash = {}
        for spec_hash in self.concretized_order:
            spec = self.specs_by_hash[spec_hash]
            new_spec = reset_os_and_compiler(spec, compiler)
            new_order.append(new_spec.dag_hash())
            new_specs_by_hash[new_spec.dag_hash()] = new_spec
        self.concretized_order = new_order
        self.specs_by_hash = new_specs_by_hash

    def _get_environment_specs(self):
        # At most one instance of any package gets added to the environment
        package_to_spec = {}

        for spec_hash in self.concretized_order:
            spec = self.specs_by_hash[spec_hash]
            for dep in spec.traverse(deptype=('link', 'run')):
                if dep.name in package_to_spec:
                    tty.warn("{0} takes priority over {1}"
                             .format(package_to_spec[dep.name].format(),
                                     dep.format()))
                else:
                    package_to_spec[dep.name] = dep

        return list(package_to_spec.values())

    def get_modules(self):
        import spack.modules

        module_files = list()
        environment_specs = self._get_environment_specs()
        for spec in environment_specs:
            module = spack.modules.LmodModule(spec)
            path = module.file_name
            if os.path.exists(path):
                module_files.append(path)
            else:
                tty.warn("Module file for {0} does not exist"
                         .format(spec.format()))

        return module_files

    def to_dict(self):
        concretized_order = list(self.concretized_order)
        common_libs = syaml.syaml_dict(self.common_libs.items())
        common_bins = syaml.syaml_dict(self.common_bins.items())
        format = {
            'user_specs': self.user_specs,
            'concretized_order': concretized_order,
            'common_libs': common_libs,
            'common_bins': common_bins
        }
        return format

    @staticmethod
    def from_dict(name, d):
        c = Context(name)
        c.user_specs = list(d['user_specs'])
        c.concretized_order = list(d['concretized_order'])
        c.common_libs = dict(d['common_libs'])
        c.common_bins = dict(d['common_bins'])
        return c

    def path(self):
        return fs.join_path(_db_dirname, self.name)

    def repo_path(self):
        return fs.join_path(self.path(), 'repo')

def reset_os_and_compiler(spec, compiler=None):
    spec = spec.copy()
    for x in spec.traverse():
        x.compiler = None
        x.architecture = None
        x.compiler_flags = FlagMap(x)
        x._concrete = False
        x._hash = None
    if compiler:
        spec.compiler = CompilerSpec(compiler)
    spec.concretize()
    return spec

def upgrade_dependency_version(spec, dep_name):
    spec = spec.copy()
    for x in spec.traverse():
        x._concrete = False
        x._hash = None
    spec[dep_name].versions = VersionList(':')
    spec.concretize()
    return spec

def write(context, new_repo=None):
    tmp_new, dest, tmp_old = write_paths(context)

    if os.path.exists(tmp_new) or os.path.exists(tmp_old):
        tty.die("Partial write state, run 'spack context repair'")

    fs.mkdirp(tmp_new)
    #create one file for the full specs in json format
    with open(fs.join_path(tmp_new, 'full_specs.json'), 'w') as F:
        store_specs_by_hash(context.specs_by_hash, F)
    #create one file for the rest of the data in yaml format
    with open(fs.join_path(tmp_new, 'context.yaml'), 'w') as F:
        syaml.dump(context.to_dict(), stream=F, default_flow_style=False)

    dest_repo_dir = fs.join_path(tmp_new, 'repo')
    if new_repo:
        shutil.copytree(new_repo.root, dest_repo_dir)
    elif os.path.exists(context.repo_path()):
        shutil.copytree(context.repo_path(), dest_repo_dir)

    if os.path.exists(dest):
        shutil.move(dest, tmp_old)
    shutil.move(tmp_new, dest)
    if os.path.exists(tmp_old):
        shutil.rmtree(tmp_old) 

def write_paths(context):
    tmp_new = fs.join_path(_db_dirname, "_" + context.name)
    dest = context.path()
    tmp_old = fs.join_path(_db_dirname, "." + context.name)
    return tmp_new, dest, tmp_old

def repair(context_name):
    """
    Possibilities:
        tmp_new, dest
        tmp_new, tmp_old
        tmp_old, dest
    """
    c = Context(context_name)
    tmp_new, dest, tmp_old = write_paths(context)
    if os.path.exists(tmp_old):
        if not os.path.exists(dest):
            shutil.move(tmp_new, dest)
        else:
            shutil.rmtree(tmp_old)
        tty.info("Previous update completed")
    elif os.path.exists(tmp_new):
        tty.info("Previous update did not complete")
    else:
        tty.info("Previous update may have completed")

    if os.path.exists(tmp_new):
        shutil.rmtree(tmp_new)

def store_specs_by_hash(specs_by_hash, stream):
    installs = dict((k, v.to_dict(all_deps=True))
                    for k, v in specs_by_hash.items())

    try:
        sjson.dump(installs, stream)
    except YAMLError as e:
        raise syaml.SpackYAMLError(
            "Error writing context full specs:", str(e))

def read(context_name):
    tmp_new, context_dir, tmp_old = write_paths(Context(context_name))

    if os.path.exists(tmp_new) or os.path.exists(tmp_old):
        tty.die("Partial write state, run 'spack context repair'")

    with open(fs.join_path(context_dir, 'context.yaml'), 'r') as F:
        context_dict = syaml.load(F)
    context = Context.from_dict(context_name, context_dict)
    with open(fs.join_path(context_dir, 'full_specs.json'), 'r') as F:
        install_dict = sjson.load(F)
    installs = dict((x, Spec.from_dict(y)) for x, y in install_dict.items())
    context.specs_by_hash = installs

    return context

def context_create(args):
    context = Context(args.context)
    if os.path.exists(context.path()):
        raise tty.die("Context already exists: " + args.context)
    write(context)

def context_add(args):
    context = read(args.context)
    for spec in spack.cmd.parse_specs(args.package):
        context.add(spec.format())
    write(context)

def context_remove(args):
    context = read(args.context)
    for spec in spack.cmd.parse_specs(args.package):
        context.remove(spec.format())
    write(context)

def context_concretize(args):
    context = read(args.context)
    repo = prepare_repository(context)

    new_specs = context.concretize()
    for spec in new_specs:
        for dep in spec.traverse():
            dump_to_context_repo(dep, repo)
    write(context, repo)

def context_install(args):
    contexts = read(args.context)
    prepare_repository(context)
    context.install()

def dump_to_context_repo(spec, repo):
    dest_pkg_dir = repo.dirname_for_package_name(spec.name)
    if not os.path.exists(dest_pkg_dir):
        spack.repo.dump_provenance(spec, dest_pkg_dir)

def prepare_repository(context, remove=None):
    import tempfile
    repo_stage = tempfile.mkdtemp()
    new_repo_dir = fs.join_path(repo_stage, 'repo')
    if os.path.exists(context.repo_path()):
        shutil.copytree(context.repo_path(), new_repo_dir)
    else:
        spack.repository.create_repo(new_repo_dir, context.name)
    if remove:
        remove_dirs = []
        repo = Repo(new_repo_dir)
        for pkg_name in remove:
            remove_dirs.append(repo.dirname_for_package_name(pkg_name))
        for d in remove_dirs:
            shutil.rmtree(d)
    repo = Repo(new_repo_dir)
    spack.repo.put_first(repo)
    return repo

def context_relocate(args):
    context = read(args.context)
    prepare_repository(context)
    context.reset_os_and_compiler(compiler=args.compiler)
    write(context)

def context_list(args):
    # TODO? option to list packages w/ multiple instances?
    context = read(args.context)
    import sys
    context.list(sys.stdout, args.include_deps)

def context_stage(args):
    context = read(args.context)
    prepare_repository(context)
    for spec in context.specs_by_hash.values():
        for dep in spec.traverse():
            dep.package.do_stage()

def context_list_modules(args):
    context = read(args.context)
    for module_file in context.get_modules():
        print(module_file)

def context_upgrade_dependency(args):
    context = read(args.context)
    repo = prepare_repository(context, [args.dep_name])
    new_dep = context.upgrade_dependency(args.dep_name, args.dry_run)
    if not args.dry_run and new_dep:
        dump_to_context_repo(new_dep, repo)
        write(context, repo)

def add_common_args(parser):
    parser.add_argument(
        'context',
        help="The context you are working with"
    )

def setup_parser(subparser):
    sp = subparser.add_subparsers(metavar='SUBCOMMAND', dest='context_command')

    create_parser = sp.add_parser('create', help='Make a context')
    add_common_args(create_parser)

    add_parser = sp.add_parser('add', help='Add a spec to a context')
    add_common_args(add_parser)
    add_parser.add_argument(
        'package',
        nargs=argparse.REMAINDER,
        help="Spec of the package to add"
    )

    remove_parser = sp.add_parser(
        'remove', help='Remove a spec from this context')
    add_common_args(remove_parser)
    remove_parser.add_argument(
        'package',
        nargs=argparse.REMAINDER,
        help="Spec of the package to remove"
    )

    concretize_parser = sp.add_parser(
        'concretize', help='Concretize user specs')
    add_common_args(concretize_parser)

    relocate_parser = sp.add_parser(
        'relocate', help='Reconcretize context with new OS and/or compiler')
    add_common_args(relocate_parser)
    relocate_parser.add_argument(
        '--compiler',
        help="Compiler spec to use"
    )

    list_parser = sp.add_parser('list', help='List specs in a context')
    list_parser.add_argument(
        '--include-deps', action='store_true',
        dest='include_deps', help='Show dependencies of requested packages')
    add_common_args(list_parser)

    modules_parser = sp.add_parser(
        'list-modules',
        help='Show modules for for packages installed in a context')
    add_common_args(modules_parser)

    upgrade_parser = sp.add_parser(
        'upgrade',
        help='Upgrade a dependency package in a context to the latest version')
    add_common_args(upgrade_parser)
    upgrade_parser.add_argument(
        'dep_name', help='Dependency package to upgrade')
    upgrade_parser.add_argument(
        '--dry-run', action='store_true', dest='dry_run',
        help="Just show the updates that would take place")

    stage_parser = sp.add_parser(
        'stage',
        help='Download all source files for all packages in a context')
    add_common_args(stage_parser)

    install_parser = sp.add_parser(
        'install',
        help='Install all concretized specs in a context')
    add_common_args(install_parser)

def context(parser, args, **kwargs):
    action = {
        'create': context_create,
        'add': context_add,
        'concretize': context_concretize,
        'list': context_list,
        'list-modules': context_list_modules,
        'remove': context_remove,
        'relocate': context_relocate,
        'upgrade': context_upgrade_dependency,
        'stage': context_stage,
        'install': context_install,
        }
    action[args.context_command](args)
