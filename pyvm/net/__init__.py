"""Network backends for the virtio-net device.

A backend moves Ethernet frames between the emulated NIC and the outside world:

    user   a NAT gateway implemented in Python (no privileges needed)  [default]
    tap    a real host TAP device (needs one to be created for you)
    none   no network device at all

`create()` builds one from a command-line spec such as

    user,hostfwd=tcp::2222-:22,hostfwd=tcp::8080-:80
    tap,ifname=pyvm0
"""

from ..errors import NetError

__all__ = ['create', 'NetError', 'describe_options']

BOOLS = {'on': True, 'yes': True, 'true': True, '1': True,
         'off': False, 'no': False, 'false': False, '0': False}

USER_OPTS = {'net': str, 'host': str, 'dns': str, 'guest': str, 'mac': str,
             'mtu': int, 'dnsserver': str, 'restrict': bool, 'debug': bool}
TAP_OPTS = {'ifname': str, 'fd': int, 'mac': str, 'mtu': int, 'debug': bool}


def _parse(spec):
    """'user,mac=...,hostfwd=...' -> (kind, {opt: value}, [hostfwd, ...])"""
    parts = [p for p in str(spec).split(',') if p.strip()]
    if not parts:
        raise NetError('empty --net specification')
    kind = parts[0].strip().lower()
    opts, fwd = {}, []
    for p in parts[1:]:
        key, eq, value = p.partition('=')
        key = key.strip().lower()
        if not eq:
            raise NetError('--net: %r should be key=value' % p)
        if key == 'hostfwd':
            fwd.append(value)
        else:
            opts[key] = value
    return kind, opts, fwd


def _convert(kind, opts, allowed):
    out = {}
    for key, value in opts.items():
        if key not in allowed:
            raise NetError('--net %s: unknown option %r (known: %s)'
                           % (kind, key, ', '.join(sorted(allowed))))
        want = allowed[key]
        if want is bool:
            if value.lower() not in BOOLS:
                raise NetError('--net %s: %s= wants on or off' % (kind, key))
            out[key] = BOOLS[value.lower()]
        elif want is int:
            try:
                out[key] = int(value, 0)
            except ValueError:
                raise NetError('--net %s: %s= wants a number' % (kind, key))
        else:
            out[key] = value
    return out


def create(spec='user', dump=None):
    """Build a backend from a spec string.  Returns None for 'none'."""
    kind, opts, fwd = _parse(spec)
    if kind in ('none', 'off'):
        if opts or fwd:
            raise NetError('--net none takes no options')
        return None
    if kind == 'user':
        from .slirp import Slirp
        kw = _convert(kind, opts, USER_OPTS)
        if 'net' in kw:
            kw['network'] = kw.pop('net')
        return Slirp(hostfwd=fwd, **kw)
    if kind == 'tap':
        from .tap import Tap
        if fwd:
            raise NetError('--net tap: hostfwd= only applies to user networking')
        return Tap(**_convert(kind, opts, TAP_OPTS))
    raise NetError('--net: unknown backend %r (want user, tap or none)' % kind)


def describe_options():
    return """\
  --net user[,OPT=VAL...]   NAT gateway in Python, no privileges needed (default)
        hostfwd=tcp::2222-:22   forward a host port into the guest (repeatable;
                                tcp or udp, host address defaults to 127.0.0.1)
        net=10.0.2.0/24         the virtual network
        host=10.0.2.2           gateway address; also means "the host"
        dns=10.0.2.3            address of the built-in DNS forwarder
        guest=10.0.2.15         address handed to the guest over DHCP
        dnsserver=1.1.1.1       resolver to forward to (default: /etc/resolv.conf)
        restrict=on             no outside access: only DHCP and DNS
        mac=52:54:00:12:34:56   guest MAC address
        mtu=1500, debug=on
  --net tap[,ifname=pyvm0][,fd=N]
                            bridge to a host TAP device (needs one made for you)
  --net none                no network device"""
