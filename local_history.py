"""Local-only Git object snapshots. Never runs Git inside a context directory."""
import difflib
import json
import os
import re
import subprocess
from pathlib import Path


class LocalHistory:
    def __init__(self, editor_dir):
        self.repo = Path(editor_dir) / 'history.git'

    def git(self, *args, data=None, check=True):
        # Ignore inherited repository/config injection and user hooks/signing.
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT='0', GIT_AUTHOR_NAME='Memory Editor',
                   GIT_AUTHOR_EMAIL='local@memory-editor.invalid',
                   GIT_COMMITTER_NAME='Memory Editor',
                   GIT_COMMITTER_EMAIL='local@memory-editor.invalid')
        command = ['git', '--no-pager', '--git-dir=' + str(self.repo),
                   '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                   '-c', 'core.logAllRefUpdates=false'] + list(args)
        try:
            result = subprocess.run(command, input=data, capture_output=True, env=env, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('Local Git history unavailable (%s)' % type(exc).__name__) from exc
        if check and result.returncode:
            raise RuntimeError('Local Git history unavailable (git exit %s)' % result.returncode)
        return result

    @staticmethod
    def path(key, target_id):
        if key not in ('memory', 'user', 'soul', 'agents') or not isinstance(target_id, str) or not re.fullmatch('[0-9a-f]{64}', target_id):
            raise ValueError('Invalid history target')
        return 'snapshots/%s/%s.md' % (target_id, key)

    def head(self):
        if not self.repo.exists():
            return None
        r = self.git('rev-parse', '--verify', 'refs/heads/main', check=False)
        if r.returncode:
            # An empty repo is fine; a broken repository must not look empty.
            self.git('rev-parse', '--is-bare-repository')
            return None
        return r.stdout.decode().strip()

    def record(self, key, target_id, raw, kind, note='', force=False):
        """Caller holds the repository lock, after canonical target locks."""
        path = self.path(key, target_id)
        if not self.repo.exists():
            self.repo.mkdir(mode=0o700)
            self.git('init', '--bare', '--template=', str(self.repo))
        os.chmod(self.repo, 0o700)
        head = self.head()
        if head and not force:
            old = self.git('show', head + ':' + path, check=False)
            if old.returncode == 0 and old.stdout == raw:
                return head
        blob = self.git('hash-object', '-w', '--stdin', data=raw).stdout.strip().decode()
        # Build the tree with plumbing only; no index, working tree, attributes,
        # credentials, remotes or directory-wide staging can enter the snapshot.
        leaf = self.git('mktree', data=('100644 blob %s\t%s.md\n' % (blob, key)).encode()).stdout.strip().decode()
        # Each target has exactly one allowed document key by target identity.
        # Preserve other targets from the last tree.
        entries = {}
        if head:
            result = self.git('ls-tree', head + ':snapshots', check=False)
            for line in result.stdout.decode().splitlines():
                meta, name = line.split('\t', 1)
                entries[name] = meta
        entries[target_id] = '040000 tree ' + leaf
        target_tree = self.git('mktree', data=''.join('%s\t%s\n' % (entries[n], n) for n in sorted(entries)).encode()).stdout.strip().decode()
        root = self.git('mktree', data=('040000 tree %s\tsnapshots\n' % target_tree).encode()).stdout.strip().decode()
        message = json.dumps({'kind': kind, 'note': note, 'target_id': target_id, 'key': key}, ensure_ascii=False).encode()
        args = ['commit-tree', root] + (['-p', head] if head else [])
        commit = self.git(*args, data=message).stdout.strip().decode()
        self.git('update-ref', 'refs/heads/main', commit, head or '0' * 40)
        return commit

    def versions(self, key, target_id):
        path = self.path(key, target_id)
        head = self.head()
        if not head:
            return []
        result = self.git('log', '--format=%H%x00%cI%x00%B%x00', head)
        fields = result.stdout.decode().split('\x00')
        versions = []
        for i in range(0, len(fields) - 2, 3):
            revision, date, message = fields[i:i+3]
            meta = json.loads(message.strip())
            if meta.get('target_id') != target_id or meta.get('key') != key:
                continue
            versions.append({'revision': revision.strip(), 'date': date,
                             'kind': meta['kind'], 'note': meta.get('note', '')})
        return versions

    def preview(self, key, target_id, revision, current):
        if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision):
            raise ValueError('Invalid revision')
        if not any(v['revision'] == revision for v in self.versions(key, target_id)):
            raise ValueError('Version is not bound to this target')
        raw = self.git('show', revision + ':' + self.path(key, target_id)).stdout
        text = raw.decode('utf-8-sig')
        lines = difflib.unified_diff(current.decode('utf-8-sig').splitlines(True),
                                     text.splitlines(True), fromfile='Current on disk',
                                     tofile='Selected version')
        diff = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                       for line in lines)
        return raw, {'revision': revision, 'content': text, 'diff': diff}
