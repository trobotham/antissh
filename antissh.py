#!/usr/bin/env python3
# dependencies: asyncssh, asyncio-irc
"""An IRC bot which monitors for compromised embedded devices being used as proxies."""


import sys
import re
import json
import socket
import logging
import pickle
import os
from configparser import ConfigParser
from typing import Dict, Tuple

import asyncio
import asyncssh
from asyncirc import irc


config = ConfigParser()
config.read(sys.argv[1])

TARGET_IP = config.get('target', 'ip', fallback='162.220.112.99')
TARGET_PORT = config.getint('target', 'port', fallback=6667)
QUICK_MODE = config.getboolean('target', 'quick_mode', fallback=False)  # type: ignore
HOST = config.get('host', 'hostname', fallback='irc.dereferenced.org')
PORT = config.getint('host', 'port', fallback=6667)
USE_SSL = config.getboolean('host', 'ssl', fallback=False)              # type: ignore
OPER = config.get('host', 'oper', fallback='x x')
NICKNAME = config.get('host', 'nickname', fallback='antissh')
SERVER_PASSWORD = config.get('host', 'password', fallback=None)         # type: ignore
MODES = config.get('host', 'modes', fallback='')
KLINE_CMD_TEMPLATE = config.get(
    'host', 'kline_cmd',
    fallback='KLINE 86400 *@{ip} :Vulnerable SSH daemon found on this host.  '
             'Please fix your SSH daemon and try again later.\r\n',
)
_BINDHOST = config.get('target', 'bindhost', fallback=None)             # type: ignore
LOG_CHAN = config.get('host', 'log_chan', fallback=None)                # type: ignore
LOG_CHAN_KEY = config.get('host', 'log_chan_key', fallback=None)        # type: ignore
CREDENTIAL_SCAN_LEVEL = config.getint('scan', 'level', fallback=1)
GEOIP_DB = config.get('geoip', 'database_path', fallback=None)          # type: ignore
GEOIP_COUNTRY_WHITELIST = frozenset(config.get('geoip', 'country_whitelist', fallback="").split())

BINDHOST = None
if _BINDHOST is not None:
    BINDHOST = (_BINDHOST, 0)

# advanced users only:
# charybdis uses:
# *** Notice -- Client connecting: kaniini_ (~kaniini@127.0.0.1) [127.0.0.1] {users} [William Pitcock]
# re.findall(r'\[[0-9a-f\.:]+\]', message)
# inspircd uses:
# *** CONNECT: Client connecting on port 6667 (class unnamed...): kaniini!kaniini@127.0.0.1 (127.0.0.1) [kaniini]
# *** REMOTECONNECT: Client connecting on port 6667 (class unnamed...): kaniini!kaniini@127.0.0.1 (127.0.0.1) [kaniini]
# re.findall(r'\([0-9a-f\.:]+\)')

IP_REGEX = re.compile(r'Client connecting.*\[([0-9a-f\.:]+)\].*{.*}.*')
POSITIVE_HIT_STRING = b'Looking up your hostname'
BASIC_CREDENTIALS = (
    ('admin', '123456'),     # huawei
)
NORMAL_CREDENTIALS = BASIC_CREDENTIALS + (
    ('ADMIN', 'ADMIN'),      # supermicro default IPMI
    ('admin', '1234'),
    ('admin', '12345'),
    ('admin', ''),
    ('root', ''),
    ('root', 'admin'),
    ('root', 'root'),
    ('root', 'changeme'),
    ('root', 'password'),
    ('root', 'calvin'),       # dell default IPMI
    ('root', 'raspberrypi'),  # default on many raspberry pi images
    ('root', 'rootme'),
    ('admin', 'admin'),
    ('admin', 'changeme'),
    ('admin', 'password'),
    ('ubnt', 'ubnt'),         # edgeos default
    ('user', 'user'),
)
DEEP_CREDENTIALS = NORMAL_CREDENTIALS + (
    ('root', 'toor'),
    ('root', 'pass'),
    ('root', '1234'),
    ('root', '12345'),
    ('root', '123456'),
    ('admin', 'abc123'),
    ('admin', 'admin123'),
    ('bitnami', 'bitnami'),
    ('cisco', 'cisco'),
    ('device', 'apc'),
    ('dpn', 'changeme'),
    ('HPSupport', 'badg3r5'),
    ('lp', 'lp'),
    ('master', 'themaster01'),
    ('osmc', 'osmc'),
    ('pi', 'raspberry'),
    ('plexuser', 'rasplex'),
    ('sysadmin', 'PASS'),
    ('user', 'live'),
    ('vagrant', 'vagrant'),
    ('virl', 'VIRL'),
    ('vyos', 'vyos'),
)
DEEPER_CREDENTIALS = DEEP_CREDENTIALS + (
    ('root', 'alien'),
    ('root', 'alpine'),        # mac os server default
    ('root', 'logapp'),
    ('root', 'openelec'),
    ('root', 'pixmet2003'),
    ('root', 'soho'),
    ('alien', 'alien'),
    ('user', 'acme'),
    ('toor', 'logapp'),
)

if CREDENTIAL_SCAN_LEVEL > 2:
    DEFAULT_CREDENTIALS = DEEPER_CREDENTIALS

elif CREDENTIAL_SCAN_LEVEL > 1:
    DEFAULT_CREDENTIALS = DEEP_CREDENTIALS

else:
    DEFAULT_CREDENTIALS = NORMAL_CREDENTIALS

# dnsbl settings
dronebl_key = config.get('dnsbl', 'dronebl_key', fallback=None)    # type: ignore
dnsbl_im_key = config.get('dnsbl', 'dnsbl_im_key', fallback=None)  # type: ignore
dnsbl_active = (dronebl_key is not None or dnsbl_im_key is not None)
if dnsbl_active:
    import aiohttp

# geoip
if GEOIP_DB and GEOIP_COUNTRY_WHITELIST:
    try:
        import geoip2.database
        geoip = geoip2.database.Reader(GEOIP_DB)
    except ModuleNotFoundError:
        print("GEOIP_* configured but missing geoip2 library: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)


def get_country_code(addr):
    """Get the ISO country code from a client IP address."""
    try:
        return geoip.country(addr).country.iso_code
    except (TypeError, NameError):
        return None


async def submit_dronebl(ip):
    """Submit detected bot to DroneBL."""
    add_stanza = '<add ip="{ip}" type="15" port="22" comment="{comment}" />'.format(
        ip=ip, comment='A vulnerable SSH server on an IOT gateway, detected by antissh.')
    envelope = '<?xml version="1.0"?><request key="{key}">{stanza}</request>'.format(
        key=dronebl_key, stanza=add_stanza)
    headers = {'Content-Type': 'text/xml'}

    async with aiohttp.ClientSession() as session:
        resp = await session.post('https://dronebl.org/RPC2', headers=headers, data=envelope)
        data = await resp.text()

        if 'success' not in data:
            print('dronebl submission error:', data)


async def submit_dnsbl_im(ip):
    """Submit detected bot to DNSBL IM."""
    envelope = {
        'key': dnsbl_im_key,
        'addresses': [{
            'ip': ip,
            'type': '4',
            'reason': 'A vulnerable SSH server on an IOT gateway, detected by antissh.',
        }],
    }
    headers = {'Content-Type': 'application/json'}

    async with aiohttp.ClientSession() as session:
        await session.post('https://api.dnsbl.im/import', headers=headers, data=json.dumps(envelope))


def log_chan(bot, msg):
    """Log a given message to channel LOG_CHAN."""
    if LOG_CHAN is not None:
        bot.writeln('PRIVMSG %s :%s' % (LOG_CHAN, msg))


cache: Dict[Tuple[str, str, int, str, str], bool] = {}
cache_fname = 'cache.pickle'


async def check_with_credentials(ip: str, target_ip: str, target_port: int, username: str, password: str) -> bool:
    """Check whether a given username or password works to open a direct TCP session."""
    key = (ip, target_ip, target_port, username, password)
    if key in cache:
        return cache[key]
    try:
        async with asyncssh.connect(
                ip, username=username, password=password,
                known_hosts=None, client_keys=None, client_host_keys=None,
                agent_path=None, local_addr=BINDHOST) as conn:
            if QUICK_MODE:
                cache[key] = True
                with open(cache_fname, 'wb') as fd:
                    pickle.dump(cache, fd)
                return True

            try:
                reader, writer = await conn.open_connection(target_ip, target_port)
            except asyncssh.Error:
                cache[key] = False
                with open(cache_fname, 'wb') as fd:
                    pickle.dump(cache, fd)
                return False

            writer.write(b'\r\n')
            writer.write_eof()

            response = await reader.read()
            cache[key] = POSITIVE_HIT_STRING in response
            with open(cache_fname, 'wb') as fd:
                pickle.dump(cache, fd)
            return POSITIVE_HIT_STRING in response
    except (asyncssh.Error, OSError):
        cache[key] = False
        with open(cache_fname, 'wb') as fd:
            pickle.dump(cache, fd)
        return False


async def fetch_banner(ip):
    """Fetch the SSH banner from a remote server."""
    reader, writer = await asyncio.open_connection(ip, 22)
    writer.write_eof()
    return (await reader.read())


async def check_with_credentials_group(ip, target_ip, target_port, credentials_group=DEFAULT_CREDENTIALS):
    """Check a list of credentials against a target system."""
    futures = asyncio.as_completed(
        map(lambda c: check_with_credentials(ip, target_ip, target_port, c[0], c[1]),
            credentials_group),
    )
    for future in futures:
        result = await future
        if result:
            return True

    return False


async def check_with_credentials_shallow(ip, target_ip, target_port):
    """Perform a shallow test against a target system."""
    # TODO: check for known bad data in kx, maybe we can avoid auth altogether
    banner = await fetch_banner(ip)

    creds = DEFAULT_CREDENTIALS

    # if dropbear, odds are likely it is a huawei device, check for that first.
    if b'dropbear' in banner:
        creds = BASIC_CREDENTIALS

    result = await check_with_credentials_group(ip, target_ip, target_port, creds)

    if result:
        return True

    # recheck if BASIC credentials check failed with full set
    if creds == BASIC_CREDENTIALS:
        result = await check_with_credentials_group(ip, target_ip, target_port)

    return bool(result)


async def check_connecting_client(bot, ip):
    """Check a newly-connected client."""
    result = await check_with_credentials_shallow(ip, TARGET_IP, TARGET_PORT)
    if result:
        try:
            ptr = socket.gethostbyaddr(ip)
        except socket.error:
            ptr = None

        ptr = "({})".format(ptr[0]) if ptr else ""

        print('found vulnerable SSH daemon at', ip, ptr)
        log_chan(bot, 'found vulnerable SSH daemon at %s %s' % (ip, ptr))

        bot.writeln(KLINE_CMD_TEMPLATE.format(ip=ip))

        if dnsbl_active:
            tasks = []
            if dronebl_key:
                tasks += [submit_dronebl(ip)]
            if dnsbl_im_key:
                tasks += [submit_dnsbl_im(ip)]
            await asyncio.wait(tasks)


def main():
    """CLI entry point for antissh."""
    logging.basicConfig(level=logging.DEBUG)
    if os.path.isfile(cache_fname):
        with open(cache_fname, 'rb') as fd:
            cache.update(pickle.load(fd))
    bot = irc.connect(HOST, PORT, use_ssl=USE_SSL)
    bot.register(NICKNAME, "antissh", "antissh proxy checking bot", password=SERVER_PASSWORD)

    @bot.on('irc-001')
    def handle_connection_start(_):
        bot.writeln("OPER {}\r\n".format(OPER))
        if MODES:
            bot.writeln("MODE {0} {1}\r\n".format(NICKNAME, MODES))
        if LOG_CHAN:
            bot.writeln("JOIN {0} {1}\r\n".format(LOG_CHAN, LOG_CHAN_KEY))
        log_chan(bot, 'antissh has started!')

    @bot.on('notice')
    def handle_connection_notice(message, user, target, text):
        if 'connecting' not in text:
            return

        match = IP_REGEX.search(text)
        if match:
            ip = match.group(1)

            if ip in ('0', '255.255.255.255', '127.0.0.1', '::1'):
                return

            cc = get_country_code(ip)
            if cc and cc in GEOIP_COUNTRY_WHITELIST:
                print("{ip} is whitelisted based on country {cc}".format(ip=ip, cc=cc))
                return

            asyncio.ensure_future(check_connecting_client(bot, ip))

    asyncio.get_event_loop().run_forever()


if __name__ == '__main__':
    main()
