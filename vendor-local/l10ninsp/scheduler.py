# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from twisted.python import log
from buildbot.scheduler import BaseUpstreamScheduler
from buildbot.sourcestamp import SourceStamp
from buildbot import buildset
from buildbot.process import properties
from buildbot.util import ComparableMixin
from twisted.internet import defer, reactor

from collections import defaultdict
from datetime import datetime
import os.path
from ConfigParser import ConfigParser
import urllib2
from django.db import connection
from life.models import Tree as ElmoTree, Repository, Forest, Push

import logger
from six.moves.urllib_parse import urljoin
import util


def timeHelper(t):
    if t is None:
        return t
    return datetime.utcfromtimestamp(t)


def try_log(f):
    def wrapped(*args, **kwargs):
        try:
            f(*args, **kwargs)
        except Exception as e:
            log.msg(e)
            raise
    return wrapped


class Tree(ComparableMixin):
    """Carry data per tree."""

    compare_attrs = ['name', 'repo', 'branches', 'l10ninis', 'all_locales',
                     'locales', 'branch2dirs', ]

    def __init__(self, name, repo, branch, l10nbranch, l10nini):
        self.name = name
        self.repo = repo
        self.branches = {'en': branch, 'l10n': l10nbranch}
        self.l10ninis = {branch: [l10nini]}
        self.all_locales = None
        self.locales = []
        self.branch2dirs = {}
        self.tld = None

    def addData(self, branch, l10nini, dirs, tld=None):
        log.msg(l10nini + ", " + str(tld))
        try:
            self.branch2dirs[branch] += dirs
        except KeyError:
            self.branch2dirs[branch] = dirs[:]
        if tld is not None:
            self.tld = tld

        if l10nini:
            if branch in self.l10ninis:
                if l10nini not in self.l10ninis[branch]:
                    self.l10ninis[branch].append(l10nini)
            else:
                self.l10ninis[branch] = [l10nini]


class AppScheduler(BaseUpstreamScheduler):
    """Scheduler used for app compare-locales builds.
    """

    compare_attrs = ('name', 'builderNames', 'treebuilder', 'inipath', 'trees')

    class BranchData:
        '''Helper class that caches the data of all trees per hg branch.
        '''
        def __init__(self):
            self.inis = defaultdict(list)
            self.dirs = defaultdict(list)
            self.topleveltrees = set()
            self.all_locales = defaultdict(set)

        def addDirs(self, tree, dirs):
            for d in dirs:
                self.dirs[d].append(tree)

    class L10nDirs(defaultdict):
        def __init__(self):
            defaultdict.__init__(self, set)

        def addDirs(self, tree, dirs):
            for d in dirs:
                self[d].add(tree)

    def __init__(self, name, builderNames, inipath, treebuildername):
        """
        @param name: the name of this Scheduler
        @param builderNames: a list of Builder names. When this Scheduler
                             decides to start a set of builds, they will be
                             run on the Builders named by this list.
        @param inipath: path to l10nbuilds.ini, describing the apps
        @param treebuildername: the name of the builder that collects
                                tree info from remote l10n.ini files
        """

        BaseUpstreamScheduler.__init__(self, name)
        self.builderNames = builderNames
        # Path to the l10nbuilds.ini file that is read synchronously
        # Can be None for testing
        if inipath is not None:
            assert os.path.exists(inipath)
        self.inipath = inipath
        self.treebuilder = treebuildername
        self.trees = {}
        # just volatile data below
        # cache tree data per hg repo branch
        self.branches = defaultdict(self.BranchData)
        self.l10nbranches = defaultdict(self.L10nDirs)
        # map tree/locale tuples to list of changes
        self.pendings = defaultdict(list)
        self.dSubmitBuildsets = None
        # deferred that's non-None if a tree builds are currently running
        self.waitOnTree = None
        self.pendingChanges = []
        self.treesToDo = set()  # trees that changed on a tree build
        self.timeout = 5
        self.headers = {
            'User-Agent': 'Elmo/1.0 (l10n.mozilla.org)'
        }

    def listBuilderNames(self):
        return self.builderNames + [self.treebuilder]

    def getPendingBuildTimes(self):
        return []

    def addTree(self, tree, changes=None):
        '''Callback that is passed to the TreeLoader step'''
        if tree.name in self.trees:
            if self.trees[tree.name] == tree:
                # we allready got that tree, all good
                logger.debug('scheduler.l10n',
                             'Tree info for %s loaded, unchanged' % tree.name)
                return
            # updated tree. Add this to treesToDo, which will be picked up
            # by checkEnUS, called after the buildset is done
            self.treesToDo.add(tree.name)
        # tree is new or changed, update django database
        forest, isnew = \
            Forest.objects.get_or_create(name=tree.branches['l10n'])
        if isnew:
            log.msg("WARNING: scheduler created forest %s, not expected" %
                    forest.name)
        try:
            tree_ = ElmoTree.objects.get(code=tree.name)
        except ElmoTree.DoesNotExist:
            tree_ = ElmoTree.objects.create(code=tree.name, l10n=forest)
        if tree_.l10n != forest:
            tree_.l10n = forest
            tree_.save()
            log.msg("scheduler updated %s.l10n to %s" %
                    (tree_.code, forest.name))
        self.trees[tree.name] = tree
        logger.debug("scheduler.l10n", "updated tree " + tree.name)
        try:
            # update caches of tree data
            self.branches.clear()
            self.l10nbranches.clear()
            for _n, _t in self.trees.iteritems():
                for _b, dirs in _t.branch2dirs.iteritems():
                    self.branches[_b].addDirs(_n, dirs)
                    self.l10nbranches[_t.branches['l10n']].addDirs(_n, dirs)
                for _b, inis in _t.l10ninis.iteritems():
                    for ini in inis:
                        self.branches[_b].inis[ini].append(_n)
                if _t.tld is not None:
                    (self.l10nbranches[_t.branches['l10n']]
                         .addDirs(_n, [_t.tld]))
                    self.branches[_t.branches['en']].topleveltrees.add(_n)
                if _t.all_locales is not None:
                    (self.branches[_t.branches['en']]
                         .all_locales[_t.all_locales]
                         .add(_n))
        except Exception, e:
            log.msg(str(e))
        logger.debug("scheduler.l10n", "branch data cache updated")

    def startService(self):
        BaseUpstreamScheduler.startService(self)
        log.msg("starting l10n scheduler")
        if self.inipath is None:
            # testing, don't trigger tree builds
            return
        # trigger tree builds for our trees, clear() first
        cp = ConfigParser()
        cp.read(self.inipath)
        self.trees.clear()
        _ds = []
        for tree in cp.sections():
            # create a BuildSet, submit it to the BuildMaster
            props = properties.Properties()
            props.update({
                    'tree': tree,
                    'l10nbuilds': self.inipath,
                    },
                         "Scheduler")
            bs = buildset.BuildSet([self.treebuilder],
                                   SourceStamp(),
                                   properties=props)
            self.submitBuildSet(bs)
            _ds.append(bs.waitUntilFinished())
        d = defer.DeferredList(_ds)
        d.addCallback(self.onTreesBuilt)
        self.waitOnTree = d

    def onTreesBuilt(self, res, branchdata=None, change=None):
        '''Callback used when all tree-builder buildsets are done.
        If change is None, this is called from startService, otherwise
        it's called as a follow up from a change-based build. If so,
        call into checkEnUS.
        After that, process all pending changes, as long as we're not
        doing more tree builds again.
        '''
        # res is either None or list of tuple build sets
        logger.debug('scheduler.l10n',
                     'pending trees got built' +
                     (change is not None and ", change given" or ""))
        # trees for the last change are built, wait no longer
        self.waitOnTree = None
        log.msg("self.branches: %s" % str(self.branches))
        log.msg("self.l10nbranches: %s" % str(self.l10nbranches))
        if change is not None and branchdata is not None:
            self.checkEnUS(res, branchdata, change)
        while self.waitOnTree is None and self.pendingChanges:
            c = self.pendingChanges.pop(0)
            self.addChange(c)

    def addChange(self, change):
        '''Main entry point for the scheduler, this is called by the
        buildmaster.
        '''
        log.msg("addChange appscheduler, %s" % str(self.waitOnTree))
        if self.waitOnTree is not None:
            # a tree build is currently running, wait with this
            # until we're done with it
            self.pendingChanges.append(change)
            return
        # fixup change.locale if property is given
        if not hasattr(change, 'locale') or not change.locale:
            if 'locale' in change.properties:
                change.locale = change.properties['locale']
        log.msg("locale: %s" % getattr(change, 'locale', 'none'))
        if not hasattr(change, 'locale') or not change.locale:
            # check branch, l10n.inis
            # if l10n.inis are found, callback to all-locales, locales/en-US
            # otherwise just check those straight away
            if change.branch not in self.branches:
                log.msg('not our branches')
                return
            tree_triggers = set()
            branchdata = self.branches[change.branch]
            for f in change.files:
                if f in branchdata.inis:
                    tree_triggers.update(branchdata.inis[f])
            if tree_triggers:
                # trigger tree builds, wait for them to finish
                # and check the change for en-US builds
                _ds = []
                for _n in tree_triggers:
                    props = properties.Properties()
                    props.update({
                            'tree': _n,
                            'l10nbuilds': self.inipath,
                            },
                                 "Scheduler")
                    bs = buildset.BuildSet([self.treebuilder],
                                           SourceStamp(branch=change.branch,
                                                       changes=[change]),
                                           properties=props)
                    self.submitBuildSet(bs)
                    _ds.append(bs.waitUntilFinished())
                d = defer.DeferredList(_ds)
                d.addCallback(self.onTreesBuilt,
                              branchdata=branchdata, change=change)
                self.waitOnTree = d
                return
            self.checkEnUS(None, branchdata, change)
            return
        # check l10n changesets
        log.msg('my branch: %s, in? %s' %
                (change.branch, ','.join(sorted(self.l10nbranches.keys()))))
        if change.branch not in self.l10nbranches:
            return
        l10ndirs = self.l10nbranches[change.branch]
        log.msg('yes, dirs: %s' % ','.join(sorted(l10ndirs)))
        trees = set()
        for f in change.files:
            for mod, _trees in l10ndirs.iteritems():
                if f.startswith(mod):
                    trees |= _trees
        for _n in trees:
            if change.locale in self.trees[_n].locales:
                self.compareBuild(_n, change.locale, [change])
            else:
                log.msg('%s not in tree %s, needs %s' % (
                    change.locale,
                    _n,
                    ','.join(sorted(self.trees[_n].locales))))
        return

    def checkEnUS(self, result, branchdata, change):
        """Factored part of change handling that's either called
        from onChange, or from onTreesBuilt.
        """
        # ignore result, either None or list of build sets
        logger.debug('scheduler.l10n',
                     'checking en-US for change %d' % change.number)
        all_locales = set()
        # pick up trees from onTreesBuilt
        en_US = set(self.treesToDo)
        self.treesToDo.clear()
        for f in change.files:
            if f in branchdata.all_locales:
                all_locales.update(branchdata.all_locales[f])
            if 'locales/en-US' in f:
                mod = f.split('locales/en-US', 1)[0]
                if mod:
                    mod = mod.rstrip('/')  # common case for non-single
                if not mod:
                    # single-module-hg, aka mobile
                    for _n in branchdata.topleveltrees:
                        for l in self.trees[_n].locales:
                            self.compareBuild(_n, l, [change])
                else:
                    if mod in branchdata.dirs:
                        en_US.update(branchdata.dirs[mod])
        # load all-locales files
        rev = 'default'
        for _n in all_locales:
            if change.revision is not None:
                rev = change.revision
            _t = self.trees[_n]
            url = urljoin(_t.repo, '{}/raw-file/{}/{}'.format(
                _t.branches['en'], rev, _t.all_locales
            ))
            request = urllib2.Request(url, headers=self.headers)
            page = urllib2.urlopen(request, timeout=self.timeout).read()
            self.onAllLocales(page, _n, change)
        # trigger all locales for all trees
        for _n in en_US:
            _t = self.trees[_n]
            for l in _t.locales:
                self.compareBuild(_n, l, [change])

    def onAllLocales(self, page, tree, change=None):
        newlocs = util.parseLocales(page)
        added = set(newlocs) - set(self.trees[tree].locales)
        logger.debug('scheduler.l10n.all-locales',
                     "had %s; got %s; new are %s" %
                     (', '.join(self.trees[tree].locales),
                      ', '.join(list(newlocs)),
                      ', '.join(list(added))))
        self.trees[tree].locales = newlocs
        for loc in added:
            self.compareBuild(tree, loc, [change])

    def compareBuild(self, tree, locale, changes):
        cs = self.pendings[(tree, locale)]
        if changes is not None:
            cs += changes
        if self.dSubmitBuildsets is None:
            self.dSubmitBuildsets = reactor.callLater(0, self.submitBuildsets)

    @try_log
    def submitBuildsets(self):
        connection.close_if_unusable_or_obsolete()
        log.msg('submitting %d pending buildsets' % len(self.pendings))
        for tpl, changes in self.pendings.iteritems():
            tree, locale = tpl
            _t = self.trees[tree]
            props = properties.Properties()
            # figure out the latest change
            try:
                when = timeHelper(max(filter(None, (c.when for c in changes))))
            except (ValueError, ImportError):
                when = None
            revisions = sorted(_t.branches.keys())
            for k, v in _t.branches.iteritems():
                _r = "000000000000"
                if k == 'l10n':
                    repo = '%s/%s' % (v, locale)
                else:
                    repo = v
                try:
                    repo = Repository.objects.get(name=repo)
                except Repository.DoesNotExist:
                    log.msg('Repository %s does not exist, skipping' % repo)
                    revisions.remove(k)
                    continue
                q = Push.objects.filter(repository=repo,
                                        changesets__branch__name='default')
                if when is not None:
                    q = q.filter(push_date__lte=when)
                try:
                    # get the latest changeset on the 'default' branch
                    #  not strictly .tip, for pushes with heads on
                    #  multiple branches (bug 602182)
                    _p = q.order_by('-pk')[0]
                    if _p.push_date:
                        if not when:
                            when = _p.push_date
                        else:
                            when = max(when, _p.push_date)
                    _c = _p.changesets.order_by('-pk')
                    _r = str(_c.filter(branch__name='default')[0].revision)
                except IndexError:
                    # no pushes, try to get a good Changeset.
                    # this is guaranteed to at least return the null changeset
                    _r = str(
                        repo.changesets
                        .filter(branch__name='default')
                        .order_by('-pk')
                        .values_list('revision', flat=True)[0])
                relpath = repo.relative_path()
                props.setProperty(k+"_branch", relpath,
                                  "Scheduler")
                if relpath != repo.name:
                    props.setProperty("local_" + repo.name, relpath,
                                      "Scheduler")
                props.setProperty(k+"_revision", _r, "Scheduler")
            _f = Forest.objects.get(name=_t.branches['l10n'])
            # use the relative path of the en repo we got above
            inipath = '{}/{}'.format(
                props['en_branch'],
                _t.l10ninis[_t.branches['en']][0])
            props.update({"tree": tree,
                          "l10nbase": _f.relative_path(),
                          "locale": locale,
                          "inipath": inipath,
                          "srctime": when,
                          "revisions": revisions,
                          },
                         "Scheduler")
            bs = buildset.BuildSet(self.builderNames,
                                   SourceStamp(changes=changes),
                                   properties=props)
            self.submitBuildSet(bs)
            log.msg('one buildset successfully submitted')
        self.dSubmitBuildsets = None
        self.pendings.clear()
