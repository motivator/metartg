from xml.etree import ElementTree
from time import time
import subprocess
import socket
import os.path
import os

from pyrrd.rrd import RRD, RRA, DS
import simplejson as json
import clustohttp
import eventlet
import memcache
import bottle
import jinja2

STATIC_PATH = '/home/synack/src/metartg/static'
TEMPLATE_PATH = '/home/synack/src/metartg/templates'

bottle.debug(True)
application = bottle.default_app()
env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATE_PATH))
cache = memcache.Client(['127.0.0.1:11211'])
gpool = eventlet.GreenPool(200)
clusto = clustohttp.ClustoProxy('http://clusto.simplegeo.com/api')
RRDPATH = '/var/lib/metartg/rrds/%(host)s/%(service)s/%(metric)s.rrd'

RRD_GRAPH_DEFS = {
    'memory': [
        'DEF:mem_free=%(rrdpath)s/mem_free.rrd:sum:AVERAGE',
        'DEF:mem_total=%(rrdpath)s/mem_total.rrd:sum:AVERAGE',
        'DEF:mem_buffers=%(rrdpath)s/mem_buffers.rrd:sum:AVERAGE',
        'CDEF:gb_mem_free=mem_free,1024,*',
        'CDEF:gb_mem_total=mem_total,1024,*',
        'CDEF:gb_mem_buffers=mem_buffers,1024,*',
        'CDEF:t_mem_used=gb_mem_total,gb_mem_free,-',
        'CDEF:gb_mem_used=t_mem_used,gb_mem_buffers,-',
        'LINE:gb_mem_total#FFFFFF:Total memory\\l',
        'AREA:gb_mem_used#006699:Used memory\\l',
    ],
    'network': [
        'DEF:net_in=%(rrdpath)s/bytes_in.rrd:sum:AVERAGE',
        'DEF:net_out=%(rrdpath)s/bytes_out.rrd:sum:AVERAGE',
        'CDEF:net_bits_in=net_in,8,*',
        'CDEF:net_bits_out=net_out,8,*',
        'LINE:net_bits_in#006699:Network in\\l',
        'LINE:net_bits_out#996600:Network out\\l',
    ],
    'cpu': [
        'DEF:cpu_user=%(rrdpath)s/cpu_user.rrd:sum:AVERAGE',
        'DEF:cpu_system=%(rrdpath)s/cpu_system.rrd:sum:AVERAGE',
        'DEF:cpu_nice=%(rrdpath)s/cpu_nice.rrd:sum:AVERAGE',
        'AREA:cpu_system#FF6600:CPU system\\l:STACK',
        'AREA:cpu_nice#FFCC00:CPU nice\\l:STACK',
        'AREA:cpu_user#FFFF66:CPU user\\l:STACK',
    ],
    'io': [
        'DEF:cpu_wio=%(rrdpath)s/cpu_wio.rrd:sum:AVERAGE',
        'LINE:cpu_wio#EA8F00:CPU iowait\\l',
    ],
    'redis-memory': [
        'DEF:memory=%(rrdpath)s/redis_used_memory.rrd:sum:AVERAGE',
        'LINE:memory#EA8F00:Redis memory\\l',
    ],
}

RRD_GRAPH_OPTIONS = {
    'cpu': ['--upper-limit', '100.0'],
    'io': ['--upper-limit', '100.0']
}

RRD_GRAPH_TITLE = {
    'network': '%(host)s | bits in/out',
    'cpu': '%(host)s | cpu %%',
    'memory': '%(host)s | memory utilization',
    'io': '%(host)s | disk i/o',
    'redis-memory': '%(host)s | redis memory',
}

RRD_GRAPH_TYPES = [
    ('cpu', 'CPU'),
    ('memory', 'Memory'),
    ('network', 'Network'),
    ('io', 'Disk I/O'),
    ('redis-memory', 'Redis memory'),
]

def get_clusto_name(dnsname):
    key = 'clusto/hostname/%s' % dnsname
    #c = cache.get(key)
    c = None
    if c:
        return c

    ip = socket.gethostbyname(dnsname)
    try:
        server = clusto.get(ip)
        hostname = server[0].attr_value(key='system', subkey='hostname')
        cache.set(key, hostname)
        return hostname
    except:
        cache.set(key, ip)
        return ip

def dumps(obj):
    callback = bottle.request.params.get('callback', None)
    result = json.dumps(obj, indent=2)

    if callback:
        bottle.response.content_type = 'text/javascript'
        result = '%s(%s)' % (callback, result)
    else:
        bottle.response.content_type = 'application/json'
    return result

def create_rrd(filename, metric, data):
    os.makedirs(os.path.dirname(filename))

    ds = [
        DS(dsName='sum', dsType=data['type'], heartbeat=600),
    ]
    rra = [
        RRA(cf='AVERAGE', xff=0.5, steps=1, rows=240),
        RRA(cf='AVERAGE', xff=0.5, steps=24, rows=240),
        RRA(cf='AVERAGE', xff=0.5, steps=168, rows=240),
        RRA(cf='AVERAGE', xff=0.5, steps=672, rows=240),
        RRA(cf='AVERAGE', xff=0.5, steps=5760, rows=240),
    ]
    rrd = RRD(filename, ds=ds, rra=rra, start=(data['ts'] - 1))
    rrd.create()

def update_rrd(filename, metric, data):
    rrd = RRD(filename)
    rrd.bufferValue(str(data['ts']), data['value'])
    rrd.update()

@bottle.post('/rrd/:host/:service')
def post_rrd_update(host, service):
    metrics = json.loads(bottle.request.body.read())
    for metric in metrics:
        rrdfile = RRDPATH % {
            'host': host,
            'service': service,
            'metric': metric,
        }
        if not os.path.exists(rrdfile):
            create_rrd(rrdfile, metric, metrics[metric])
            bottle.response.status = 201
        update_rrd(rrdfile, metric, metrics[metric])
    return

@bottle.get('/graph/:cluster/:host/:graphtype')
def get_graph(cluster, host, graphtype):
    now = int(time())
    params = bottle.request.params
    start = params.get('start', (now - 3600))
    end = params.get('end', now)
    size = params.get('size', 'large')

    cmd = ['/usr/bin/rrdtool', 'graph',
        '-',
        '--font', 'DEFAULT:7:monospace',
        '--font-render-mode', 'normal',
        '--color', 'MGRID#880000',
        '--color', 'GRID#777777',
        '--color', 'CANVAS#000000',
        '--color', 'FONT#ffffff',
        '--color', 'BACK#444444',
        '--color', 'SHADEA#000000',
        '--color', 'SHADEB#000000',
        '--color', 'FRAME#444444',
        '--color', 'ARROW#FFFFFF',
        '--imgformat', 'PNG',
        '--tabwidth', '75',
        '--start', str(start),
        '--end', str(end),
    ]

    hostname = get_clusto_name(host)
    cmd += RRD_GRAPH_OPTIONS.get(graphtype, [])
    cmd += ['--title', RRD_GRAPH_TITLE.get(graphtype, hostname) % {'host': hostname}]

    if size == 'small':
        cmd += [
            '--no-legend',
            '--width', '375',
            '--height', '100'
        ]
    else:
        cmd += [
            '--width', '600',
            '--height', '200',
            '--watermark', 'simplegeo'
        ]

    for gdef in RRD_GRAPH_DEFS.get(graphtype, []):
        cmd.append(gdef % {
            'rrdpath': '/var/lib/ganglia/rrds/%s/%s' % (cluster, host),
            'cluster': cluster,
            'host': host,
        })
    #print ' '.join(cmd)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, env={'TZ': 'PST8PDT'})
    stdout, stderr = proc.communicate()

    bottle.response.content_type = 'image/png'
    return stdout

@bottle.get('/static/:filename')
def server_static(filename):
    return bottle.static_file(filename, root=STATIC_PATH)

@bottle.get('/search')
def search():
    p = bottle.request.params
    query = p.get('q', None)
    if not query:
        bottle.abort(400, 'Parameter "q" is required')

    pools = query.replace('+', ' ').replace(',', ' ').split(' ')
    pools.sort()

    cachekey = 'search/%s' % ','.join(pools)
    #result = cache.get(cachekey)
    result = None
    if result:
        return dumps(json.loads(result))

    def get_contents(name):
        obj = clusto.get_by_name(name)
        return set(obj.contents())

    pools = list(gpool.imap(get_contents, pools))
    first = pools[0]
    pools = pools[1:]

    result = []
    servers = gpool.imap(lambda x: (x, x.attrs()), first.intersection(*pools))

    def get_server_info(server):
        return {
            'name': server.name,
            'parents': [x.name for x in server.parents()],
            'contents': [x.name for x in server.contents()],
            'ip': server.attr_values(key='ip', subkey='ipstring'),
            'dnsname': server.attr_values(key='ec2', subkey='public-dns'),
        }

    servers = list(gpool.imap(get_server_info, first.intersection(*pools)))
    cache.set(cachekey, json.dumps(servers))

    return dumps(servers)

@bottle.get('/')
def index():
    template = env.get_template('search.html')
    return template.render(graphtypes=RRD_GRAPH_TYPES)

if __name__ == '__main__':
    bottle.run()