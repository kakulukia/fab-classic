"""
Classes and subroutines dealing with network connections and related topics.
"""

from functools import wraps
import getpass
import os
import re
import time
import socket
import sys
from io import StringIO

import paramiko as ssh

from fabric.auth import get_password, set_password
from fabric.utils import handle_prompt_abort, warn
from fabric.exceptions import NetworkError


ipv6_regex = re.compile(
    r'^\[?(?P<host>[0-9A-Fa-f:]+(?:%[a-z]+\d+)?)\]?(:(?P<port>\d+))?$')


def direct_tcpip(client, host, port):
    return client.get_transport().open_channel(
        'direct-tcpip',
        (host, int(port)),
        ('', 0)
    )


def is_key_load_error(e):
    return (
        e.__class__ is ssh.SSHException
        and 'Unable to parse key file' in str(e)
    )


def _tried_enough(tries):
    from fabric.state import env
    return tries >= env.connection_attempts


def get_gateway(host, port, cache, replace=False):
    """
    Create and return a gateway socket, if one is needed.

    This function checks ``env`` for gateway or proxy-command settings and
    returns the necessary socket-like object for use by a final host
    connection.

    :param host:
        Hostname of target server.

    :param port:
        Port to connect to on target server.

    :param cache:
        A ``HostConnectionCache`` object, in which gateway ``SSHClient``
        objects are to be retrieved/cached.

    :param replace:
        Whether to forcibly replace a cached gateway client object.

    :returns:
        A ``socket.socket``-like object, or ``None`` if none was created.
    """
    from fabric.state import env, output
    sock = None
    conf = ssh_config()
    proxy_command = conf.get('proxycommand', None)
    proxy_jump = conf.get('proxyjump', None)
    # only support simple single ProxyJump, not comma-separated list
    # precedence: env.gateway, ProxyJump, ProxyCommand
    gateway = env.gateway or proxy_jump
    if gateway:
        gateway = normalize_to_string(gateway)
        # ensure initial gateway connection
        if replace or gateway not in cache:
            if output.debug:
                print("Creating new gateway connection to %r" % gateway)
            cache[gateway] = connect(*normalize(gateway) + (cache, False))
        # now we should have an open gw connection and can ask it for a
        # direct-tcpip channel to the real target. (bypass cache's own
        # __getitem__ override to avoid hilarity - this is usually called
        # within that method.)
        sock = direct_tcpip(dict.__getitem__(cache, gateway), host, port)
    elif proxy_command:
        sock = ssh.ProxyCommand(proxy_command)
    return sock


class HostConnectionCache(dict):
    """
    Dict subclass allowing for caching of host connections/clients.

    This subclass will intelligently create new client connections when keys
    are requested, or return previously created connections instead.

    It also handles creating new socket-like objects when required to implement
    gateway connections and `ProxyCommand`, and handing them to the inner
    connection methods.

    Key values are the same as host specifiers throughout Fabric: optional
    username + ``@``, mandatory hostname, optional ``:`` + port number.
    Examples:

    * ``example.com`` - typical Internet host address.
    * ``firewall`` - atypical, but still legal, local host address.
    * ``user@example.com`` - with specific username attached.
    * ``bob@smith.org:222`` - with specific nonstandard port attached.

    When the username is not given, ``env.user`` is used. ``env.user``
    defaults to the currently running user at startup but may be overwritten by
    user code or by specifying a command-line flag.

    Note that differing explicit usernames for the same hostname will result in
    multiple client connections being made. For example, specifying
    ``user1@example.com`` will create a connection to ``example.com``, logged
    in as ``user1``; later specifying ``user2@example.com`` will create a new,
    2nd connection as ``user2``.

    The same applies to ports: specifying two different ports will result in
    two different connections to the same host being made. If no port is given,
    22 is assumed, so ``example.com`` is equivalent to ``example.com:22``.
    """
    def connect(self, key):
        """
        Force a new connection to ``key`` host string.
        """
        from fabric.state import env

        user, host, port = normalize(key)
        key = normalize_to_string(key)
        seek_gateway = True
        # break the loop when the host is gateway itself
        if env.gateway:
            seek_gateway = normalize_to_string(env.gateway) != key
        self[key] = connect(
            user, host, port, cache=self, seek_gateway=seek_gateway)

    def __getitem__(self, key):
        """
        Autoconnect + return connection object
        """
        key = normalize_to_string(key)
        if key not in self:
            self.connect(key)
        return dict.__getitem__(self, key)

    #
    # Dict overrides that normalize input keys
    #

    def __setitem__(self, key, value):
        return dict.__setitem__(self, normalize_to_string(key), value)

    def __delitem__(self, key):
        return dict.__delitem__(self, normalize_to_string(key))

    def __contains__(self, key):
        return dict.__contains__(self, normalize_to_string(key))


def ssh_config(host_string=None):
    """
    Return ssh configuration dict for current env.host_string host value.

    Memoizes the loaded SSH config file, but not the specific per-host results.

    This function performs the necessary "is SSH config enabled?" checks and
    will simply return an empty dict if not. If SSH config *is* enabled and the
    value of env.ssh_config_path is not a valid file, it will abort.

    May give an explicit host string as ``host_string``.
    """
    from fabric.state import env
    dummy = {}
    if not env.use_ssh_config:
        return dummy
    if '_ssh_config' not in env:
        try:
            conf = ssh.SSHConfig()
            path = os.path.expanduser(env.ssh_config_path)
            with open(path) as fd:
                conf.parse(fd)
                env._ssh_config = conf
        except IOError:
            warn("Unable to load SSH config file '%s'" % path)
            return dummy
    host = parse_host_string(host_string or env.host_string)['host']
    return env._ssh_config.lookup(host)


def key_filenames():
    """
    Returns list of SSH key filenames for the current env.host_string.

    Takes into account ssh_config and env.key_filename, including normalization
    to a list. Also performs ``os.path.expanduser`` expansion on any key
    filenames.
    """
    from fabric.state import env
    keys = env.key_filename
    # For ease of use, coerce stringish key filename into list
    if isinstance(env.key_filename, str) or env.key_filename is None:
        keys = [keys]
    # Strip out any empty strings (such as the default value...meh)
    keys = list(filter(bool, keys))
    # Honor SSH config
    conf = ssh_config()
    if 'identityfile' in conf:
        # Assume a list here as we require Paramiko 1.10+
        keys.extend(conf['identityfile'])
    return list(map(os.path.expanduser, keys))


def key_from_env(passphrase=None):
    """
    Returns a paramiko-ready key from a text string of a private key
    """
    from fabric.state import env, output

    if 'key' in env:
        if output.debug:
            # NOTE: this may not be the most secure thing; OTOH anybody running
            # the process must by definition have access to the key value,
            # so only serious problem is if they're logging the output.
            sys.stderr.write("Trying to honor in-memory key %r\n" % env.key)
        for pkey_class in (ssh.rsakey.RSAKey, ssh.dsskey.DSSKey):
            if output.debug:
                sys.stderr.write("Trying to load it as %s\n" % pkey_class)
            try:
                return pkey_class.from_private_key(StringIO(env.key), passphrase)
            except Exception as e:
                # File is valid key, but is encrypted: raise it, this will
                # cause cxn loop to prompt for passphrase & retry
                if 'Private key file is encrypted' in str(e):
                    raise
                # Otherwise, it probably means it wasn't a valid key of this
                # type, so try the next one.
                else:
                    pass


def parse_host_string(host_string):
    # Split host_string to user (optional) and host/port
    user_hostport = host_string.rsplit('@', 1)
    hostport = user_hostport.pop()
    user = user_hostport[0] if user_hostport and user_hostport[0] else None

    # Split host/port string to host and optional port
    # For IPv6 addresses square brackets are mandatory for host/port separation
    if hostport.count(':') > 1:
        # Looks like IPv6 address
        r = ipv6_regex.match(hostport).groupdict()
        host = r['host'] or None
        port = r['port'] or None
    else:
        # Hostname or IPv4 address
        host_port = hostport.rsplit(':', 1)
        host = host_port.pop(0) or None
        port = host_port[0] if host_port and host_port[0] else None

    return {'user': user, 'host': host, 'port': port}


def normalize(host_string, omit_port=False):
    """
    Normalizes a given host string, returning explicit host, user, port.

    If ``omit_port`` is given and is True, only the host and user are returned.

    This function will process SSH config files if Fabric is configured to do
    so, and will use them to fill in some default values or swap in hostname
    aliases.

    Regarding SSH port used:

    * Ports explicitly given within host strings always win, no matter what.
    * When the host string lacks a port, SSH-config driven port configurations
      are used next.
    * When the SSH config doesn't specify a port (at all - including a default
      ``Host *`` block), Fabric's internal setting ``env.port`` is consulted.
    * If ``env.port`` is empty, ``env.default_port`` is checked (which should
      always be, as one would expect, port ``22``).
    """
    from fabric.state import env
    # Gracefully handle "empty" input by returning empty output
    if not host_string:
        return ('', '') if omit_port else ('', '', '')
    # Parse host string (need this early on to look up host-specific ssh_config
    # values)
    r = parse_host_string(host_string)
    host = r['host']

    # Env values (using defaults if somehow earlier defaults were replaced with
    # empty values)
    user = env.user or env.local_user

    # SSH config data
    conf = ssh_config(host_string)
    # Only use ssh_config values if the env value appears unmodified from
    # the true defaults. If the user has tweaked them, that new value
    # takes precedence.
    if user == env.local_user and 'user' in conf:
        user = conf['user']

    # Also override host if needed
    if 'hostname' in conf:
        host = conf['hostname']
    # Merge explicit user/port values with the env/ssh_config derived ones
    # (Host is already done at this point.)
    user = r['user'] or user

    if omit_port:
        return user, host

    # determine port from ssh config if enabled
    ssh_config_port = None
    if env.use_ssh_config:
        ssh_config_port = conf.get('port', None)

    # port priority order (as in docstring)
    port = r['port'] or ssh_config_port or env.port or env.default_port

    return user, host, port


def to_dict(host_string):
    user, host, port = normalize(host_string)
    return {
        'user': user, 'host': host, 'port': port, 'host_string': host_string
    }


def from_dict(arg):
    return join_host_strings(arg['user'], arg['host'], arg['port'])


def denormalize(host_string):
    """
    Strips out default values for the given host string.

    If the user part is the default user, it is removed;
    if the port is port 22, it also is removed.
    """
    from fabric.state import env

    r = parse_host_string(host_string)
    user = ''
    if r['user'] is not None and r['user'] != env.user:
        user = r['user'] + '@'
    port = ''
    if r['port'] is not None and r['port'] != '22':
        port = ':' + r['port']
    host = r['host']
    host = '[%s]' % host if port and host.count(':') > 1 else host
    return user + host + port


def join_host_strings(user, host, port=None):
    """
    Turns user/host/port strings into ``user@host:port`` combined string.

    This function is not responsible for handling missing user/port strings;
    for that, see the ``normalize`` function.

    If ``host`` looks like IPv6 address, it will be enclosed in square brackets

    If ``port`` is omitted, the returned string will be of the form
    ``user@host``.
    """
    if port:
        # Square brackets are necessary for IPv6 host/port separation
        template = "%s@[%s]:%s" if host.count(':') > 1 else "%s@%s:%s"
        return template % (user, host, port)
    else:
        return "%s@%s" % (user, host)


def normalize_to_string(host_string):
    """
    normalize() returns a tuple; this returns another valid host string.
    """
    return join_host_strings(*normalize(host_string))


def connect(user, host, port, cache, seek_gateway=True):
    """
    Create and return a new SSHClient instance connected to given host.

    :param user: Username to connect as.

    :param host: Network hostname.

    :param port: SSH daemon port.

    :param cache:
        A ``HostConnectionCache`` instance used to cache/store gateway hosts
        when gatewaying is enabled.

    :param seek_gateway:
        Whether to try setting up a gateway socket for this connection. Used so
        the actual gateway connection can prevent recursion.
    """
    from fabric.state import env, output

    #
    # Initialization
    #

    # Init client
    client = ssh.SSHClient()

    # Load system hosts file (e.g. /etc/ssh/ssh_known_hosts)
    known_hosts = env.get('system_known_hosts')
    if known_hosts:
        client.load_system_host_keys(known_hosts)

    # Load known host keys (e.g. ~/.ssh/known_hosts) unless user says not to.
    if not env.disable_known_hosts:
        client.load_system_host_keys()
    # Unless user specified not to, accept/add new, unknown host keys
    if not env.reject_unknown_hosts:
        client.set_missing_host_key_policy(ssh.AutoAddPolicy())

    #
    # Connection attempt loop
    #

    # Initialize loop variables
    connected = False
    password = get_password(user, host, port, login_only=True)
    tries = 0
    sock = None

    # Loop until successful connect (keep prompting for new password)
    while not connected:
        # Attempt connection
        try:
            tries += 1

            # (Re)connect gateway socket, if needed.
            # Nuke cached client object if not on initial try.
            if seek_gateway:
                sock = get_gateway(host, port, cache, replace=tries > 0)

            # Set up kwargs (this lets us skip GSS-API kwargs unless explicitly
            # set; otherwise older Paramiko versions will be cranky.)
            kwargs = dict(
                hostname=host,
                port=int(port),
                username=user,
                password=password,
                pkey=key_from_env(password),
                key_filename=key_filenames(),
                timeout=env.timeout,
                banner_timeout=env.banner_timeout,
                auth_timeout=env.auth_timeout,
                allow_agent=not env.no_agent,
                look_for_keys=not env.no_keys,
                sock=sock,
            )
            for suffix in ('auth', 'deleg_creds', 'kex'):
                name = "gss_" + suffix
                val = env.get(name, None)
                if val is not None:
                    kwargs[name] = val

            # Ready to connect
            client.connect(**kwargs)
            connected = True

            # set a keepalive if desired
            if env.keepalive:
                client.get_transport().set_keepalive(env.keepalive)

            return client
        # BadHostKeyException corresponds to key mismatch, i.e. what on the
        # command line results in the big banner error about man-in-the-middle
        # attacks.
        except ssh.BadHostKeyException as e:
            raise NetworkError(
                ("Host key for %s did not match pre-existing key!"
                 " Server's key was changed recently,"
                 " or possible man-in-the-middle attack.") % host, e)
        # Prompt for new password to try on auth failure
        except (
            ssh.AuthenticationException,
            ssh.PasswordRequiredException,
            ssh.SSHException
        ) as e:
            msg = str(e)
            # If we get SSHExceptionError and the exception message indicates
            # SSH protocol banner read failures, assume it's caused by the
            # server load and try again.
            #
            # If we are using a gateway, we will get a ChannelException if
            # connection to the downstream host fails. We should retry.
            if (e.__class__ is ssh.SSHException and msg.startswith('Error reading SSH protocol banner')) \
               or e.__class__ is ssh.ChannelException:
                if _tried_enough(tries):
                    raise NetworkError(msg, e)
                continue

            # For whatever reason, empty password + no ssh key or agent
            # results in an SSHException instead of an
            # AuthenticationException. Since it's difficult to do
            # otherwise, we must assume empty password + SSHException ==
            # auth exception.
            #
            # Conversely: if we get SSHException and there
            # *was* a password -- it is probably something non auth
            # related, and should be sent upwards. (This is not true if the
            # exception message does indicate key parse problems.)
            #
            # This also holds true for rejected/unknown host keys: we have to
            # guess based on other heuristics.
            if (
                e.__class__ is ssh.SSHException
                and (
                    password
                    or msg.startswith('Unknown server')
                    or "not found in known_hosts" in msg
                )
                and not is_key_load_error(e)
            ):
                raise NetworkError(msg, e)

            # Otherwise, assume an auth exception, and prompt for new/better
            # password.

            # Paramiko doesn't handle prompting for locked private
            # keys (i.e.  keys with a passphrase and not loaded into an agent)
            # so we have to detect this and tweak our prompt slightly.
            # (Otherwise, however, the logic flow is the same, because
            # ssh's connect() method overrides the password argument to be
            # either the login password OR the private key passphrase. Meh.)
            #
            # NOTE: This will come up if you normally use a
            # passphrase-protected private key with ssh-agent, and enter an
            # incorrect remote username, because ssh.connect:
            # * Tries the agent first, which will fail as you gave the wrong
            # username, so obviously any loaded keys aren't gonna work for a
            # nonexistent remote account;
            # * Then tries the on-disk key file, which is passphrased;
            # * Realizes there's no password to try unlocking that key with,
            # because you didn't enter a password, because you're using
            # ssh-agent;
            # * In this condition (trying a key file, password is None)
            # ssh raises PasswordRequiredException.
            text = None
            if isinstance(e, ssh.PasswordRequiredException) or is_key_load_error(e):
                # NOTE: we can't easily say WHICH key's passphrase is needed,
                # because ssh doesn't provide us with that info, and
                # env.key_filename may be a list of keys
                text = "[%s] Passphrase for private key" % env.host_string
            else:
                print("Connect error: %s" % e)
            password = prompt_for_password(text)
            # Update env.password, env.passwords if empty
            set_password(user, host, port, password)
        # Ctrl-D / Ctrl-C for exit
        # TODO: this may no longer actually serve its original purpose and may
        # also hide TypeErrors from paramiko. Double check in v2.
        except (EOFError, TypeError):
            # Print a newline (in case user was sitting at prompt)
            print('')
            sys.exit(0)
        # Handle DNS error / name lookup failure
        except socket.gaierror as e:
            raise NetworkError('Name lookup failed for %s' % host, e)
        # Handle timeouts and retries, including generic errors
        # NOTE: In 2.6, socket.error subclasses IOError
        except socket.error as e:
            not_timeout = type(e) is not socket.timeout
            giving_up = _tried_enough(tries)
            # Baseline error msg for when debug is off
            msg = "Timed out trying to connect to %s" % host
            # Expanded for debug on
            err = msg + " (attempt %s of %s)" % (tries, env.connection_attempts)
            if giving_up:
                err += ", giving up"
            err += ")"
            # Debuggin'
            if output.debug:
                sys.stderr.write(err + '\n')
            # Having said our piece, try again
            if not giving_up:
                # Sleep if it wasn't a timeout, so we still get timeout-like
                # behavior
                if not_timeout:
                    time.sleep(env.timeout)
                continue
            # Override eror msg if we were retrying other errors
            if not_timeout:
                msg = "Low level socket error connecting to host %s on port %s: %s" % (
                    host, port, e.args[1]
                )
            # Here, all attempts failed. Tweak error msg to show # tries.
            # TODO: find good humanization module, jeez
            s = "s" if env.connection_attempts > 1 else ""
            msg += " (tried %s time%s)" % (env.connection_attempts, s)
            raise NetworkError(msg, e)
        # Ensure that if we terminated without connecting and we were given an
        # explicit socket, close it out.
        finally:
            if not connected and sock is not None:
                sock.close()


def _password_prompt(prompt, stream):
    return getpass.getpass(prompt, stream)

def prompt_for_password(prompt=None, no_colon=False, stream=None):
    """
    Prompts for and returns a new password if required; otherwise, returns
    None.

    A trailing colon is appended unless ``no_colon`` is True.

    If the user supplies an empty password, the user will be re-prompted until
    they enter a non-empty password.

    ``prompt_for_password`` autogenerates the user prompt based on the current
    host being connected to. To override this, specify a string value for
    ``prompt``.

    ``stream`` is the stream the prompt will be printed to; if not given,
    defaults to ``sys.stderr``.
    """
    from fabric.state import env
    handle_prompt_abort("a connection or sudo password")
    stream = stream or sys.stderr
    # Construct prompt
    default = "[%s] Login password for '%s'" % (env.host_string, env.user)
    password_prompt = prompt if (prompt is not None) else default
    if not no_colon:
        password_prompt += ": "
    # Get new password value
    new_password = _password_prompt(password_prompt, stream)
    # Otherwise, loop until user gives us a non-empty password (to prevent
    # returning the empty string, and to avoid unnecessary network overhead.)
    while not new_password:
        print("Sorry, you can't enter an empty password. Please try again.")
        new_password = _password_prompt(password_prompt, stream)
    return new_password


def needs_host(func):
    """
    Prompt user for value of ``env.host_string`` when ``env.host_string`` is
    empty.

    This decorator is basically a safety net for silly users who forgot to
    specify the host/host list in one way or another. It should be used to wrap
    operations which require a network connection.

    Due to how we execute commands per-host in ``main()``, it's not possible to
    specify multiple hosts at this point in time, so only a single host will be
    prompted for.

    Because this decorator sets ``env.host_string``, it will prompt once (and
    only once) per command. As ``main()`` clears ``env.host_string`` between
    commands, this decorator will also end up prompting the user once per
    command (in the case where multiple commands have no hosts set, of course.)
    """
    from fabric.state import env
    @wraps(func)
    def host_prompting_wrapper(*args, **kwargs):
        while not env.get('host_string', False):
            handle_prompt_abort("the target host connection string")
            prompt = "No hosts found. Please specify (single) " \
                "host string for connection: "
            host_string = input(prompt)
            env.update(to_dict(host_string))
        return func(*args, **kwargs)
    host_prompting_wrapper.undecorated = func
    return host_prompting_wrapper


def disconnect_all():
    """
    Disconnect from all currently connected servers.

    Used at the end of ``fab``'s main loop, and also intended for use by
    library users.
    """
    from fabric.state import connections, output
    # Explicitly disconnect from all servers
    for key in list(connections.keys()):
        if output.status:
            # Here we can't use the py3k print(x, end=" ")
            # because 2.5 backwards compatibility
            sys.stdout.write("Disconnecting from %s ... " % denormalize(key))
        connections[key].close()
        del connections[key]
        if output.status:
            sys.stdout.write("done.\n")
