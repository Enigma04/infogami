"""
bulkupload script to upload multiple objects at once. 
All the inserts are merged to give better performance.
"""
import web
from infobase import TYPES, DATATYPE_REFERENCE
import datetime
import re

def sqlin(name, values):
    """
        >>> sqlin('id', [1, 2, 3, 4])
        <sql: 'id IN (1, 2, 3, 4)'>
        >>> sqlin('id', [])
        <sql: '1 = 2'>
    """
    def sqljoin(queries, sep):
        result = ""
        for q in queries:
            if result:
                result = result + sep
            result = result + q
        return result
    
    if not values:
        return web.reparam('1 = 2', {})
    else:
        values = [web.reparam('$v', locals()) for v in values]
        return name + ' IN ('+ sqljoin(values, ", ") + ')'

@web.memoize        
def get_table_columns(table):
    # Postgres query to get all column names. 
    # Got by runing sqlalchemy with echo=True.
    q = """
    SELECT a.attname,
      pg_catalog.format_type(a.atttypid, a.atttypmod),
      (SELECT substring(d.adsrc for 128) FROM pg_catalog.pg_attrdef d
       WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef)
      AS DEFAULT,
      a.attnotnull, a.attnum, a.attrelid as table_oid
    FROM pg_catalog.pg_attribute a
    WHERE a.attrelid = (
        SELECT c.oid
        FROM pg_catalog.pg_class c
             LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE (pg_catalog.pg_table_is_visible(c.oid))
             AND c.relname = $table AND c.relkind in ('r','v')
    ) AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum;
    """ 
    return [r.attname for r in web.query(q, locals())]
    
def multiple_insert(table, values, seqname=None):
    """Inserts multiple rows into a table using sql copy."""
    def escape(value):
        if value is None:
            return "\N"
        elif isinstance(value, basestring): 
            value = value.replace('\\', r'\\') # this must be the first one
            value = value.replace('\t', r'\t')
            value = value.replace('\r', r'\r')
            value = value.replace('\n', r'\n')
            return value
        elif isinstance(value, bool):
            return value and 't' or 'f'
        else:
            return str(value)
            
    def increment_sequence(seqname, n):
        """Increments a sequence by the given amount."""
        d = web.query(
            "SELECT setval('%s', $n + (SELECT last_value FROM %s), true) + 1 - $n AS START" % (seqname, seqname), 
            locals())
        return d[0].start
        
    def write(path, data):
        f = open(path, 'w')
        f.write(web.utf8(data))
        f.close()
        
    if not values:
        return []
        
    if seqname is None:
        seqname = table + "_id_seq"
        
    #print "inserting %d rows into %s" % (len(values), table)
        
    columns = get_table_columns(table)
    if seqname:
        n = len(values)
        start = increment_sequence(seqname, n)
        ids = range(start, start+n)
        for v, id in zip(values, ids):
            v['id'] = id
    else:
        ids = None
        
    data = []
    for v in values:
        assert set(v.keys()) == set(columns)
        data.append("\t".join([escape(v[c]) for c in columns]))
    write("/tmp/data.copy", "\n".join(data))
    web.query("COPY %s FROM '/tmp/data.copy'" % table)
    return ids
    
def get_key2id():
    """Return key to id mapping for all things in the database."""
    key2id = {}
    offset = 0
    limit = 100000
    while True:
        result = web.query('SELECT id, key FROM thing ORDER BY id LIMIT $limit OFFSET $offset', vars=locals())
        if not result:
            break
        for row in result:
            key2id[row.key] = row.id
        offset = len(key2id)
        
    return key2id

key2id = None
        
class BulkUpload:
    def __init__(self, site, author=None, comment=None, machine_comment=None):
        self.site = site
        self.author_id = author and author.id
        self.comment = {}
        self.machine_comment = {}
        self.created = []
        self.now = datetime.datetime.utcnow().isoformat()

        # initialize key2id, if it is not initialzed already.
        global key2id
        key2id = key2id or get_key2id()
        
    def upload(self, query):
        """Inserts"""
        assert isinstance(query, list)
        web.transact()
        try:
            self.process_creates(query)
            self.process_inserts(query)
        except:
            web.rollback()
            raise
        else:
            web.commit()
            
    def process_creates(self, query):
        keys = set(self.find_keys(query))
        tobe_created = set(self.find_creates(query))
        tobe_created = [k for k in tobe_created if k not in key2id] 
        
        assert '/type/author' in keys        
        assert '/type/author' in key2id
    
        # insert things
        d = dict(site_id=self.site.id, created=self.now, last_modified=self.now, latest_revision=1, deleted=False)
        values = [dict(d, key=k) for k in tobe_created]
        ids = multiple_insert('thing', values)
        for v, id in zip(values, ids):
            key2id[v['key']] = id
        
        # insert versions
        d = dict(created=self.now, revision=1, author_id=self.author_id, ip=None, comment=self.comment, machine_comment=self.machine_comment)    
        multiple_insert('version', [dict(d, thing_id=key2id[k], comment=self.comment[k], machine_comment=self.machine_comment[k]) for k in tobe_created])
        self.created = tobe_created
        
    def find_keys(self, query, result=None):
        if result is None:
            result = []
            
        if isinstance(query, list):
            for q in query: 
                self.find_keys(q, result)
        elif isinstance(query, dict) and 'key' in query:
            assert re.match('^/[^ \t\n]*$', query['key']), 'Bad key: ' + repr(query['key'])
            result.append(query['key'])
            for k, v in query.items():
                self.find_keys(v, result)
        return result
    
    def find_creates(self, query, result=None):
        """Find keys of all queries which have 'create' key.
        """
        if result is None: 
            result = []
            
        if isinstance(query, list):
            for q in query:
                self.find_creates(q, result)
        elif isinstance(query, dict):
            if 'create' in query:
                result.append(query['key'])
                self.find_creates(query.values(), result)
                #@@ side-effect
                self.comment[query['key']] = query.pop('comment', None)
                self.machine_comment[query['key']] = query.pop('machine_comment', None) 
        return result
        
    def process_inserts(self, query):
        values = []
        for q in query:
            self.prepare_datum(q, values)
        multiple_insert('datum', values, seqname=False)
        
    def prepare_datum(self, query, result, path=""):
        """This is a funtion with side effect. 
        It append values to be inserted to datum table into result and return (value, datatype) for that query.
        """
        max_rev = 2 ** 31 - 1
        def append(thing_id, key, value, datatype, ordering):
            result.append(dict(
                thing_id=thing_id, 
                begin_revision=1,
                end_revision = max_rev, 
                key=key, 
                value=value, 
                datatype=datatype, 
                ordering=ordering))
        
        if isinstance(query, dict):
            if 'value' in query:
                return (query['value'], TYPES[query['type']])
            else:
                thing_id = key2id[query['key']]
                if query['key'] in self.created:
                    for key, value in query.items():
                        if key == 'create': 
                            continue
                        if isinstance(value, list):
                            for i, v in enumerate(value):
                                _value, datatype = self.prepare_datum(v, result, "%s/%s#%d" % (path, key, i))
                                append(thing_id, key, _value, datatype, i)
                        else:
                            _value, datatype = self.prepare_datum(value, result, "%s/%s" % (path, key))
                            append(thing_id, key, _value, datatype, None)
                return (thing_id, DATATYPE_REFERENCE)
        elif isinstance(query, basestring):
            return (query, TYPES['/type/string'])
        elif isinstance(query, int):
            return (query, TYPES['/type/int'])
        elif isinstance(query, float):
            return (query, TYPES['/type/float'])
        elif isinstance(query, bool):
            return (int(query), TYPES['/type/boolean'])
        else:
            raise Exception, '%s: invalid value: %s' (path, repr(query))

if __name__ == "__main__":
    import sys
    
    web.config.db_parameters = dict(dbn='postgres', db='infobase', user='anand', pw='') 
    web.config.db_printing = True
    web.load()
    from infobase import Infobase
    site = Infobase().get_site('infogami.org')
    
    def book(i):
        return {
            'create': 'unless_exists',
            'type': '/type/book',
            'key': u'/b/b%d' % i,
            'title': "title-%d" % i,
            'description': {'type': '/type/text', 'value': 'description-%d' % i},
            'author': {'create': 'unless_exists', 'type': {'key': '/type/author'}, 'key': '/a/a%d' % i, 'name': 'author %d' % i},
            'publisher': {'create': 'unless_exists', 'key': '/p/%d' % i, 'name': 'publisher %d' % i},
            'comment': 'comment for book#%d' % i,
            'machine_comment': 'machine_comment for book#%d' % i,            
        }
        
    if len(sys.argv) > 1:
        n = int(sys.argv[1])
    else:
        n = 100

    if len(sys.argv) > 2:
        times = int(sys.argv[2])
    else:
        times = 10
        
    site.write({'create': 'unless_exists', 'key': '/type/book', 'type': '/type/type'})
    site.write({'create': 'unless_exists', 'key': '/type/author', 'type': '/type/type'})
            
    start = web.query('select last_value from thing_id_seq')[0].last_value
    print "database has %d things" % start
        
    import time

    for i in range(times):
        t1 = time.time()
        q = [book(start+i) for i in range(n)]
        start += n
        BulkUpload(site).upload(q)
        t2 = time.time()
        t = t2 - t1
        print "inserting %d records took %f seconds (%f records/sec)" % (n, t, n/t)