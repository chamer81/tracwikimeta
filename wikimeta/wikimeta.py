# Wikimeta plugin

import re
import time

from genshi.builder import tag
from genshi.filters.transform import Transformer
from genshi.core import Markup

from trac.core import *
from trac.web import IRequestHandler
from trac.web.chrome import INavigationContributor, ITemplateProvider, add_stylesheet
from trac.env import *
from trac.db.api import DatabaseManager
from trac.db.schema import Table, Column
from trac.web.api import IRequestFilter, ITemplateStreamFilter
from trac.wiki.api import IWikiChangeListener, IWikiPageManipulator, IWikiSyntaxProvider
from trac.wiki.api import WikiSystem
from trac.wiki.formatter import system_message, HtmlFormatter
from trac.wiki.macros import WikiMacroBase
from trac.wiki.model import WikiPage
from trac.util import get_reporter_id
from trac.mimeview import Context
from trac.resource import Resource, render_resource_link, get_resource_url
from trac.perm import IPermissionPolicy, IPermissionRequestor
from trac.perm import PermissionError, PermissionSystem

from tractags.model import tag_resource


PLUGIN_NAME = 'WikiMetaPlugin'
PLUGIN_DB_VERSION = 1
PLUGIN_SCHEMA = [
    Table('wikimeta', key=['name', 'owner', 'state', 'time'])[
        Column('name', type='text'),
        Column('owner', type='text'),
        Column('state', type='text'),
        Column('priority', type='int(11)'),
        Column('time', type='bigint(20)'),
        Column('author', type='text'),
        Column('current', type='int(11)')],
    Table('tags_category', key=('category', 'tag'))[
        Column('category'),
        Column('tag')]
    ]

STATES = [ 'planned', 'nice to have', 'current', 'obsolete' ]

def _create_select(label_text, id, name, options, selected_name=None, default_selection=None):
    select = tag.select(id=id, name=name)
    if selected_name is None and default_selection is not None:
        selected_name = default_selection
    for option_name in options:
        if option_name == selected_name:
            select.append(tag.option(option_name, value=option_name, selected='selected'))
        else:
            select.append(tag.option(option_name, value=option_name))
    insert = tag(label_text)
    insert(
        tag.br(), select
    )
    insert = tag.div(tag.label(insert), class_='field')
    return insert



class PageMeta:
    def __init__(self, name, owner, state, priority, time, author):
        self.name = name
        self.owner = owner
        self.state = state
        self.priority = priority
        self.time = time
        self.author = author
        self.items = {}

    def __getitem__(self, key):
        if key in self.items:
            return self.items[key]
        return None

    def __setitem__(self, key, value):
        self.items[key] = value

    def __getitems__(self):
        self.items['name'] = self.name
        self.items['owner'] = self.owner
        self.items['state'] = self.state
        self.items['priority'] = self.priority
        self.items['time'] = self.time
        self.items['author'] = self.author
        return self.items.copy()

    def _get_tags(self, env):
        tags = []
        db = env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("""
            SELECT tag FROM tags where tagspace='wiki' and name=%s group by tag
        """, (self.name))
        for row in cursor:
            tags.append(row[0])
        return tags

    def save(self, env, old_meta):
        env.log.debug(' +++ save: %s' % self.name)
        if old_meta is not None:
            self.priority = old_meta.priority
            if self.owner == old_meta.owner and self.state == old_meta.state:
                env.log.debug(' +++ existing meta equals old meta: %s, %s' % (old_meta.owner, old_meta.state))
                return False
        else:
            env.log.debug(' +++ no existing meta, saving: %s' % self.name)
        self.insert(env)
        return True

    def insert(self, env):
        """insert the wiki meta data in the database."""
        #env.log.debug(' +++ in insert')
        db = env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("""
                UPDATE wikimeta set current=0 where name=%s 
            """, (self.name))
        if self.state == 'planned':
            if self.priority == 0:
                cursor.execute("""
                    INSERT into wikimeta (name, owner, state, priority, time, author, current)
                    select %s, %s, %s, ifnull(max(priority) + 1,1), %s, %s, 1 from wikimeta where current=1
                """, (self.name, self.owner, self.state, time.time(), self.author))
            else:
                cursor.execute("""
                    INSERT into wikimeta (name, owner, state, priority, time, author, current)
                    values (%s, %s, %s, %s, %s, %s, 1) 
                """, (self.name, self.owner, self.state, self.priority, time.time(), self.author))
        else:
            cursor.execute("""
                INSERT into wikimeta (name, owner, state, priority, time, author, current)
                values (%s, %s, %s, %s, %s, %s, 1) 
            """, (self.name, self.owner, self.state, 0, time.time(), self.author))
        db.commit()
        #env.log.debug(' +++ done saving state: %s' % self.state)

class WikiMetaPlugin(Component):
    implements(INavigationContributor, IRequestHandler, ITemplateProvider, 
        IEnvironmentSetupParticipant, ITemplateStreamFilter,
        IWikiPageManipulator, IWikiChangeListener, IRequestFilter, 
        IPermissionRequestor, IPermissionPolicy)

    # IRequestFilter methods
    def pre_process_request(self, req, handler):
        self.log.debug(" +++ in pre_process_request")
        #self.log.debug(dir(req.args))
        #self.log.debug(req.args)
        if req and req.path_info.startswith('/wiki') and 'save' in req.args and 'state_name' in req.args and 'page' in req.args:
            page_meta = PageMeta(req.args.get('page'), req.args.get('owner_name'), req.args.get('state_name'), 0, time.time(), get_reporter_id(req, 'author'))
            if page_meta.save(self.env, self._get_page_meta(req.args.get('page'))):
                req.args.__setitem__('wikimeta', 'updated')
        #self.log.debug(" +++ in pre_process_request, done")
        return handler

    def post_process_request(self, req, template, data, content_type):
        self.log.debug(" +++ in post_process_request")
        return (template, data, content_type)

    # ITemplateStreamFilter methods
    def filter_stream(self, req, method, filename, stream, data):
        #self.log.debug(" +++ in modified filter_stream")
        if filename == 'wiki_edit.html':
            return self._wiki_edit(req, req.path_info, stream)
        else:
            return stream | Transformer('//img').attr('alt', 'NOT wiki edit')


    # internal (for now)

    def _wiki_edit(self, req, path_info, stream):
        #self.log.debug(" +++ in _wiki_edit for path_info: %s" % path_info)
        if 'page' not in req.args:
            return stream
        currently_logged_in_user = get_reporter_id(req, 'author')
        owner = currently_logged_in_user
        state = STATES[0]
        page_name = req.args.get('page')
        page_meta = self._get_page_meta(page_name)
        if page_meta is not None and len(page_meta.owner) > 0:
            owner = page_meta.owner
            state = page_meta.state
        user_tuples = self.env.get_known_users()
        user_ids = [item[0] for item in user_tuples]
        select_state = _create_select('state', 'state_id', 'state_name', STATES, state, 'planned')
        select_owner = _create_select('owner', 'owner_id', 'owner_name', user_ids, owner, 'dybuster')
        return stream | Transformer('//div[@id="changeinfo1"]').prepend(select_owner).prepend(select_state)

    def _get_page_meta(self, name):
        """Return meta information for a wiki page."""
        self.log.debug(" +++ in _get_page_meta for %s" % name)
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("""
            SELECT owner, state, priority, time, author FROM wikimeta WHERE name=%s and current=1
        """, (name))
        for row in cursor:
            self.log.debug(" +++ in _get_page_meta, found row with state %s" % row[1])
            return PageMeta(name, row[0], row[1], int(row[2]), int(row[3]), row[4])
        self.log.debug(" +++ in _get_page_meta, no row for %s" % name)
        return None

    def _get_categorized_tags(self):
        tags = {}
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("""
            SELECT tag, category FROM tags_category order by tag
        """)
        for row in cursor:
            if row[1] in tags.keys():
                tags[row[1]].append(row[0])
            else:
                tags[row[1]] = [ row[0] ]
        # now get the uncategorized:
        uncategorized = []
        cursor.execute("""
            select tag from tags where tag not in (select tag from tags_category) group by tag
        """)
        for row in cursor:
            uncategorized.append(row[0])
        if len(uncategorized) > 0:
            tags['uncategorized'] = uncategorized
        return tags

    # IPermissionRequestor method
    def get_permission_actions(self):
        return ['WIKIMETA_VIEW']

    # IPermissionPolicy method
    def check_permission(self, action, username, resource, perm):
        self.log.debug(" +++ check_permission, action: %s" % action)
        return True


    # IWikiPageManipulator methods
    def prepare_wiki_page(self, req, page, fields):
        self.log.debug(" +++ in prepare_wiki_page")
        pass

    def validate_wiki_page(self, req, page):
        self.log.debug(" +++ in validate_wiki_page")
        #self.log.debug(dir(req.args))
        #self.log.debug(req.args)
        # If the page hasn't been modified but the meta has, 
        # redirect to avoid the "page has not been modified" warning.
        if req and req.path_info.startswith('/wiki') and 'save' in req.args:
            page_modified = req.args.get('text') != page.old_text or \
                    page.readonly != int('readonly' in req.args)
            if 'wikimeta' in req.args and not page_modified:
                    req.redirect(get_resource_url(self.env, page.resource,
                                                  req.href, version=None))
        return []

    # IWikiChangeListener methods
    def wiki_page_added(self, page):
        self.log.debug(" +++ in wiki_page_added")

    def wiki_page_changed(self, page, version, t, comment, author, ipnr):
        self.log.debug(" +++ in wiki_page_changed")

    def wiki_page_renamed(self, page, old_name):
        self.log.debug(" +++ in wiki_page_renamed")
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("""
                UPDATE wikimeta set name=%s where name=%s
                """, (page.name, old_name))
        db.commit()

    def wiki_page_deleted(self, page):
        self.log.debug(" +++ in wiki_page_deleted")
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("""
                UPDATE wikimeta set current=0 where name=%s
                """, (page.name))
        db.commit()

    def wiki_page_version_deleted(self, page):
        self.log.debug(" +++ in wiki_page_version_deleted")

    # INavigationContributor methods
    def get_active_navigation_item(self, req):
        if 'WIKIMETA_VIEW' in req.perm:
            return 'wikimeta'
    
    def get_navigation_items(self, req):
        if 'WIKIMETA_VIEW' in req.perm:
            yield ('mainnav', 'wikimeta', tag.a('WikiFilter', href=req.href.wikimeta()))
    
    # IRequestHandler methods
    def match_request(self, req):
        return req.path_info.startswith('/wikimeta')
    
    def process_request(self, req):
        req.perm.require('WIKIMETA_VIEW')
        
        data = {}
        self.log.debug(" +++ in process_request")
        # first process reordering:
        for key in req.args.keys():
            if key.startswith('reorder_'):
                self.log.debug(" +++ found reorder: %s" % key)
                reorder_args = key.split('_')
                self._priority_reorder(int(reorder_args[1]), int(reorder_args[2]))

        # data for state filter
        data['state_label'] = 'State:'
        data['state_options'] = ['all (non-obsolete)'] + STATES
        data['selected_state'] = 'all (non-obsolete)'
        if req.args.get('state_name') is not None:
            data['selected_state'] = req.args.get('state_name')
        # data for owner filter
        data['owner_label'] = 'Owner:'
        owner_tuples = self.env.get_known_users()
        data['owner_options'] = ['all'] + [item[0] for item in owner_tuples]
        data['selected_owner'] = 'all'
        if req.args.get('owner_name') is not None:
            data['selected_owner'] = req.args.get('owner_name')

        # data for tag filters
        combined_title = ""
        categorized_tags = self._get_categorized_tags()
        tag_states = []
        tag_list = []

        for category in categorized_tags.keys():
            tag_states.append([category, 'category'])
            for tag in categorized_tags[category]:
                if req.args.get('tagfilter_%s' % tag) is not None:
                    tag_states.append([tag, 'checked'])
                    tag_list.append(tag)
                    combined_title = '%s %s' % (tag, combined_title)
                else:
                    tag_states.append([tag, 'unchecked'])
        data['tags'] = tag_states
        data['tags_label'] = 'Tag Filter'
        if len(combined_title) == 0:
            data['combined_title'] = 'all'
        else:
            data['combined_title'] = combined_title

        # check if the user requested to add a new page:
        newpagename = req.args.get('newpagename')
        if newpagename is not None and len(newpagename) > 0:
            newpage = WikiPage(self.env, newpagename)
            currently_logged_in_user = get_reporter_id(req, 'author')
            if data['selected_owner'] == 'all':
                new_owner = currently_logged_in_user
            else:
                new_owner = data['selected_owner']
            if newpage.exists == False:
                newpage.text = 'page content goes here'
                newpage.save(author=currently_logged_in_user, comment='', remote_addr='127.0.0.1')
            # add tags and meta
            if data['selected_state'] == 'all (non-obsolete)':
                new_state = 'planned'
            else:
                new_state = data['selected_state']
            new_page_meta = PageMeta(newpagename, new_owner, new_state, 0, time.time(), currently_logged_in_user)
            new_page_meta.insert(self.env)
            tag_resource(self.env, newpage.resource, old_id=None, author=currently_logged_in_user, tags=tag_list)

        # get a top context to render the wiki data:
        context = Context.from_request(req, 'wiki')
        self.log.debug(" +++ context: ")
        self.log.debug(dir(context))
        self.log.debug(" +++ child: ")
        self.log.debug(context.child)
        self.log.debug(" +++ href: ")
        self.log.debug(context.href)
        self.log.debug(" +++ parent: ")
        self.log.debug(context.parent)

        # get the wiki pages:
        wiki_data = self._get_wiki_data(context, data['selected_state'], data['selected_owner'], tag_list)
        #self.log.debug(" +++ wiki_data:")
        #self.log.debug(wiki_data)
        data['wiki_data'] = wiki_data

        add_stylesheet(req, 'wm/css/wikimeta.css')
        # This tuple is for Genshi (template_name, data, content_type)
        # Without data the trac layout will not appear.
        return 'wikimeta.html', data, None

    # find a good name that could be used for a new wiki page:
    def _get_unused_title(self, tag_list):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        if len(tag_list) == 0:
            base = 'Misc'
        else:
            base = ''
            for tag in tag_list:
                base = '%s%s' % (base, tag.capitalize())
        for index in range(1,5000):
            name = '%s%d' % (base, index)
            cursor.execute("""
                SELECT * 
                FROM wiki WHERE name=%s 
            """, (name))
            row = cursor.fetchone()
            if not row:
                return name

    # Fetch all page data, depending on the filters:
    def _get_wiki_data(self, context, selected_state, selected_owner, tag_list):
        """Get all page data for pages matching criteria."""
        #self.log.debug(" +++ in _get_wiki_data")
        page_metas = []
        page_datas = {}
        returnList = []
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("""
                SELECT name, owner, state, priority, time, author 
                FROM wikimeta WHERE current=1 
                order by priority desc, time desc
            """)
        for row in cursor:
            #self.log.debug(" +++ in _get_page_meta, found row with state %s" % row[2])
            if selected_owner == 'all' or selected_owner == row[1]:
                if selected_state == row[2] or (selected_state == 'all (non-obsolete)' and 'obsolete' != row[2]):
                    page_metas.append(PageMeta(row[0], row[1], row[2], int(row[3]), int(row[4]), row[5]))
        for page_meta in page_metas:
            page_tags = page_meta._get_tags(self.env)
            if set(tag_list).issubset(page_tags):
                # passes all tests, so get the data:
                page = WikiPage(self.env, page_meta.name)
                if page is not None:
                    page_data = page_meta.__getitems__()
                    page_data['html'] = Markup(HtmlFormatter(self.env, context('wiki', page_meta.name), page.text).generate())
                    page_data['tags'] = page_tags
                    page_data['last_modified'] = page.time.strftime("%Y.%m.%d")
                    returnList.append(page_data)
        if len(returnList) > 1 and selected_state == 'planned':
            for index in range(0,len(returnList)):
                if index > 0:
                    returnList[index]['raisable'] = 'True'
                    returnList[index]['prev_priority'] = returnList[index - 1]['priority']
                if index < len(returnList) - 1:
                    returnList[index]['lowerable'] = 'True'
                    returnList[index]['next_priority'] = returnList[index + 1]['priority']
        return returnList

    def _priority_reorder(self, initial_priority, change_to_priority):
        db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("""
                UPDATE wikimeta set priority=-2 where priority=%s
                """, (initial_priority))

        if initial_priority > change_to_priority:
            cursor.execute("""
                UPDATE wikimeta set priority=(priority + 1) where priority<=%s and priority>=%s
                """, (initial_priority, change_to_priority))
        else:
            cursor.execute("""
                UPDATE wikimeta set priority=(priority - 1) where priority<=%s and priority>=%s
                """, (change_to_priority, initial_priority))

        cursor.execute("""
                UPDATE wikimeta set priority=%s where priority=-2
                """, (change_to_priority))

        db.commit()


    # ITemplateProvider methods
    # Used to add the plugin's templates and htdocs 
    def get_templates_dirs(self):
        from pkg_resources import resource_filename
        return [resource_filename(__name__, 'templates')]

    def get_htdocs_dirs(self):
        """Return a list of directories with static resources (such as style
        sheets, images, etc.)

        Each item in the list must be a `(prefix, abspath)` tuple. The
        `prefix` part defines the path in the URL that requests to these
        resources are prefixed with.

        The `abspath` is the absolute path to the directory containing the
        resources on the local file system.
        """
        from pkg_resources import resource_filename
        return [('wm', resource_filename(__name__, 'htdocs'))]


    # IEnvironmentSetupParticipant methods

    def environment_created(self):
        pass

    def environment_needs_upgrade(self, db):
        dbm = DatabaseManager(self.env)
        pluginName = 'wikimeta_version'

        self.log.debug(" +++ in environment_needs_upgrade")
        self.log.debug(" +++ dbm: ")
        self.log.debug(dir(dbm))

        schema_ver = self.get_schema_version(db=db, pluginName=pluginName)

        self.log.debug(" +++ schema_ver: ")
        self.log.debug(schema_ver)

        tags_version = self.get_schema_version(db=db, pluginName='tags_version')
        if tags_version == 0:
            self.log.debug(" Error: This plugin requires the TracTags plugin")
            return False

        if schema_ver == PLUGIN_DB_VERSION:
            return False
        else:
            return True

    def upgrade_environment(self, db):
        dbm = DatabaseManager(self.env)
        pluginName = 'wikimeta_version'
        schema_ver = self.get_schema_version(db=db, pluginName=pluginName)

        if schema_ver == 0:
            dbm.create_tables(PLUGIN_SCHEMA)

            cursor = db.cursor()

            cursor.execute("""
                INSERT into system (name, value)
                   values (%s,%s)
                """, (pluginName, PLUGIN_DB_VERSION))
            self.log.info("initialized wikimeta db schema: %d to %d"
                      % (schema_ver, PLUGIN_DB_VERSION))
            db.commit()

    # upgrade-related methods copied from tags plugin:
    def get_schema_version(self, db=None, pluginName='wikimeta_version'):
        """Return the current schema version for this plugin."""
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("""
            SELECT value
              FROM system
             WHERE name=%s
        """, pluginName)
        row = cursor.fetchone()
        if not row:
            return 0
        return int(row[0])
 
    def _get_tables(self, dburi, cursor):
        """Code from TracMigratePlugin by Jun Omae (see tracmigrate.admin)."""
        if dburi.startswith('sqlite:'):
            sql = """
                SELECT name
                  FROM sqlite_master
                 WHERE type='table'
                   AND NOT name='sqlite_sequence'
            """
        elif dburi.startswith('postgres:'):
            sql = """
                SELECT tablename
                  FROM pg_tables
                 WHERE schemaname = ANY (current_schemas(false))
            """
        elif dburi.startswith('mysql:'):
            sql = "SHOW TABLES"
        else:
            raise TracError('Unsupported database type "%s"'
                            % dburi.split(':')[0])
        cursor.execute(sql)
        return sorted([row[0] for row in cursor])

