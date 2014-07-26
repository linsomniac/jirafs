import argparse
import json
import logging
import os
import subprocess
import sys
import time
import webbrowser

from blessings import Terminal
import ipdb
import six
from six.moves import configparser
from six.moves.urllib import parse

from . import constants
from . import utils
from .exceptions import (
    GitCommandError,
    LocalCopyOutOfDate,
    NotTicketFolderException
)
from .ticketfolder import TicketFolder


logger = logging.getLogger(__name__)


COMMANDS = {}


def command(desc, name=None, try_subfolders=False, aliases=None):
    def decorator(func):
        func_name = name or func.__name__
        func.description = desc
        func.try_subfolders = try_subfolders
        COMMANDS[func_name] = func
        if aliases:
            for alias in aliases:
                COMMANDS[alias] = func
        return func
    return decorator


def short_status_line(folder):
    return (
        "On ticket {ticket} ({url})".format(
            ticket=folder.ticket_number,
            url=folder.cached_issue.permalink(),
        )
    )


@command('Fetch remote changes', try_subfolders=True)
def fetch(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.parse_args(args)

    folder = TicketFolder(path, jira)
    folder.fetch()


@command('Merge remote changes into your local copy', try_subfolders=True)
def merge(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.parse_args(args)

    folder = TicketFolder(path, jira)
    folder.merge()


@command('Fetch and apply remote changes locally', try_subfolders=True)
def pull(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.parse_args(args)

    folder = TicketFolder(path, jira)
    folder.pull()


@command('Commit local changes for later pushing to JIRA')
def commit(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--message', default='Untitled')
    args, extra = parser.parse_known_args(args)

    kwargs = {}
    if args.message:
        kwargs['message'] = args.message

    folder = TicketFolder(path, jira)
    try:
        folder.commit(args.message, *extra)
    except subprocess.CalledProcessError:
        print("No changes to commit")


@command('Push local changes to JIRA', try_subfolders=True)
def push(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.parse_args(args)

    folder = TicketFolder(path, jira)
    try:
        folder.push()
    except LocalCopyOutOfDate:
        print(
            "Your local copy is out-of-date; please run "
            "`jirafs merge` to merge changes from JIRA."
        )


@command('Run a command in this issue\'s git repository')
def git(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    _, extra = parser.parse_known_args(args)

    folder = TicketFolder(path, jira)
    print(folder.run_git_command(*extra))


@command('Print the log for this issue')
def log(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.parse_known_args(args)

    folder = TicketFolder(path, jira)
    print(folder.get_log())


@command('Open debug console')
def debug(args, jira, path, **kwargs):
    folder = TicketFolder(path, jira)
    ipdb.set_trace()


@command('Get the status of the current folder', try_subfolders=True)
def status(args, jira, path, **kwargs):
    t = Terminal()

    def format_field_changes(changes, color):
        lines = []
        color = getattr(t, color)
        normal = t.normal

        for filename in changes['files']:
            lines.append(
                '\t' + color + filename + normal + ' (file upload)'
            )
        for field, value_set in changes['fields'].items():
            lines.append(
                '\t' + color + field + normal +
                ' (field changed from \'%s\' to \'%s\')' % value_set
            )
        if changes['new_comment']:
            lines.append(
                '\t' + color + '[New Comment]' + normal
            )
            for line in changes['new_comment'].split('\n'):
                lines.append(
                    '\t\t' + line
                )

        return '\n'.join(lines)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--format',
        default='text',
        choices=['text', 'json']
    )
    args = parser.parse_args(args)

    folder = TicketFolder(path, jira)
    if args.format == 'json':
        print(json.dumps(folder.status()))
    else:
        print(short_status_line(folder))
        folder_status = folder.status()
        if not folder_status['up_to_date']:
            print(
                t.magenta + "Warning: unmerged upstream changes exist; "
                "run `jirafs merge` to merge them into your local copy." +
                t.normal
            )

        printed_changes = False
        ready = folder_status['ready']
        if ready['files'] or ready['fields'] or ready['new_comment']:
            printed_changes = True
            print('')
            print(
                "Ready for upload; use `jirafs push` to update JIRA."
            )
            print(
                format_field_changes(ready, 'green')
            )

        staged = folder_status['uncommitted']
        if staged['files'] or staged['fields'] or staged['new_comment']:
            printed_changes = True
            print('')
            print(
                "Uncommitted changes; use `jirafs commit` to mark these "
                "for JIRA."
            )
            print(
                format_field_changes(staged, 'red')
            )

        if not printed_changes:
            print('No changes found')
        else:
            print('')


@command(
    'Clone a new ticket folder for the specified ticket number',
    aliases=['get'],
)
def clone(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'ticket_url',
        nargs=1,
        type=six.text_type
    )
    parser.add_argument(
        'path',
        nargs='*',
        type=six.text_type,
    )
    args = parser.parse_args(args)
    ticket_url = args.ticket_url[0]
    ticket_url_parts = parse.urlparse(ticket_url)
    if not ticket_url_parts.netloc:
        default_server = utils.get_default_jira_server()
        ticket_url = parse.urljoin(
            default_server,
            'browse/' + ticket_url + '/'
        )
    path = args.path[0] if args.path else None

    TicketFolder.clone(
        path=path,
        ticket_url=ticket_url,
        jira=jira,
    )


@command('Open this ticket in JIRA', try_subfolders=True)
def open(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.parse_args(args)

    folder = TicketFolder(path, jira)

    webbrowser.open(folder.cached_issue.permalink())


@command('Show local issue changes')
def diff(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.parse_args(args)

    folder = TicketFolder(path, jira)
    result = folder.run_git_command('diff')
    if result.strip():
        print(result)


@command('Get or set configuration values')
def config(args, jira, path, **kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--get', action='store_true')
    parser.add_argument('--set', action='store_true')
    parser.add_argument('--global', dest='global_config', action='store_true')
    parser.add_argument('params', nargs='*')
    args = parser.parse_args(args)
    if not args.list and not args.get and not args.set:
        parser.error(
            'Please specify action using either --list, '
            '--set, or --get.'
        )

    if args.global_config:
        config = utils.get_config()
    else:
        try:
            folder = TicketFolder(path, jira)
            config = folder.get_config()
        except NotTicketFolderException:
            config = utils.get_config()

    if args.list:
        if len(args.params) != 0:
            parser.error(
                "--list requires no parameters."
            )
        for section in config.sections():
            parameters = config.items(section)
            for key, value in parameters:
                line = (
                    "{section}.{key}={value}".format(
                        section=section,
                        key=key,
                        value=value
                    )
                )
                print(line)
    elif args.get:
        if len(args.params) != 1:
            parser.error(
                "--get requires exactly one parameter, the configuration "
                "value to display."
            )
        section, key = args.params[0].rsplit('.', 1)
        try:
            print(config.get(section, key))
        except configparser.Error:
            pass
    elif args.set:
        if len(args.params) != 2:
            parser.error(
                "--set requires exactly two parameters, the configuration "
                "key, and the configuration value."
            )
        section, key = args.params[0].rsplit('.', 1)
        value = args.params[1]

        if args.global_config:
            config = utils.get_config()
            if not config.has_section(section):
                config.add_section(section)
            config.set(section, key, value)
            with open(
                os.path.expanduser('~/%s' % constants.GLOBAL_CONFIG)
            ) as out:
                config.write(out)
        else:
            try:
                folder = TicketFolder(path, jira)
                folder.set_config_value(
                    section, key, value
                )
            except NotTicketFolderException:
                parser.error(
                    "Not currently within a ticket folder.  To set a "
                    "global configuration value, use the --global option."
                )


def main():
    parser = argparse.ArgumentParser(
        description='Edit Jira issues locally from your filesystem',
    )
    parser.add_argument(
        'command',
        nargs=1,
        type=six.text_type,
        choices=COMMANDS.keys()
    )
    args, extra = parser.parse_known_args()

    command_name = args.command[0]
    fn = COMMANDS[command_name]
    started = time.time()
    logger.debug(
        'Command %s(%s) started',
        command_name,
        extra
    )
    jira = utils.lazy_get_jira()
    try:
        fn(extra, jira=jira, path=os.getcwd())
    except GitCommandError as e:
        print(
            "Error (code: %s) while running git command." % (
                e.returncode
            )
        )
        print("")
        print("Command:")
        print("    %s" % e.command)
        print("")
        print("Output:")
        for line in e.output.decode('utf8').split('\n'):
            print("    %s" % line)
        print("")
        sys.exit(1)
    except NotTicketFolderException:
        if not fn.try_subfolders:
            print(
                "The command '%s' must be ran from within an issue folder." % (
                    command_name
                )
            )
            sys.exit(1)
        count_runs = 0
        for folder in os.listdir(os.getcwd()):
            try:
                fn(
                    extra,
                    jira=jira,
                    path=os.path.join(
                        os.getcwd(),
                        folder,
                    ),
                )
                count_runs += 1
            except NotTicketFolderException:
                pass
        if count_runs == 0:
            print(
                "The command '%s' must be ran from within an issue folder "
                "or from within a folder containing issue folders." % (
                    command_name
                )
            )
            sys.exit(1)

    logger.debug(
        'Command %s(%s) finished in %s seconds',
        command_name,
        extra,
        (time.time() - started)
    )
