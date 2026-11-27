"""Durably consume synthetic Unreal world IDs before a demo can order."""
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3

_WORLD = re.compile(r'[a-fA-F0-9]{32}\Z', re.ASCII)
_APP_ID = 0x41475752


class TeamShelfWorldRegistry:
    """One stable file fences reusing the same simulated world after restart.

    All launches must use the same file. Deleting or replacing it destroys the
    fence and must never be treated as safe recovery of an old Unreal world.
    """
    def __init__(self, path):
        path = Path(path)
        if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
            raise ValueError('absolute stable registry path required')
        self.path = path
        created = False
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            created = True
        except FileExistsError:
            if not path.is_file() or path.is_symlink():
                raise ValueError('regular registry file required')
        stat = path.stat()
        self._identity = (stat.st_dev, stat.st_ino)
        try:
            with closing(self._open()) as db:
                db.execute('BEGIN IMMEDIATE')
                if created:
                    db.execute('PRAGMA application_id=%d' % _APP_ID)
                    db.execute('PRAGMA user_version=2')
                    db.execute('CREATE TABLE consumed_worlds (world_id TEXT PRIMARY KEY NOT NULL)')
                    db.execute('CREATE TABLE persistent_worlds (world_id TEXT PRIMARY KEY NOT NULL '
                               'REFERENCES consumed_worlds(world_id), owner_id TEXT UNIQUE NOT NULL)')
                elif db.execute('PRAGMA user_version').fetchone() == (1,):
                    self._verify_v1(db)
                    db.execute('CREATE TABLE persistent_worlds (world_id TEXT PRIMARY KEY NOT NULL '
                               'REFERENCES consumed_worlds(world_id), owner_id TEXT UNIQUE NOT NULL)')
                    db.execute('PRAGMA user_version=2')
                self._verify(db)
                db.commit()
        except sqlite3.Error as exc:
            raise ValueError('synthetic world registry is unavailable or invalid') from exc

    def _open(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('world registry unavailable')
        stat = self.path.stat()
        if (stat.st_dev, stat.st_ino) != self._identity:
            raise ValueError('world registry was replaced')
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=5)
        stat = self.path.stat()
        if (stat.st_dev, stat.st_ino) != self._identity:
            db.close()
            raise ValueError('world registry was replaced')
        return db

    @staticmethod
    def _verify_v1(db):
        if (db.execute('PRAGMA application_id').fetchone() != (_APP_ID,)
                or db.execute('PRAGMA user_version').fetchone() != (1,)
                or db.execute('PRAGMA quick_check').fetchall() != [('ok',)]
                or db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                    != [('consumed_worlds',)]
                or db.execute('PRAGMA table_info(consumed_worlds)').fetchall()
                    != [(0, 'world_id', 'TEXT', 1, None, 1)]):
            raise ValueError('synthetic world registry is invalid')

    @staticmethod
    def _verify(db):
        if (db.execute('PRAGMA application_id').fetchone() != (_APP_ID,)
                or db.execute('PRAGMA user_version').fetchone() != (2,)
                or db.execute('PRAGMA quick_check').fetchall() != [('ok',)]
                or db.execute('PRAGMA foreign_key_check').fetchone() is not None
                or db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                    != [('consumed_worlds',), ('persistent_worlds',)]
                or db.execute('PRAGMA table_info(consumed_worlds)').fetchall()
                    != [(0, 'world_id', 'TEXT', 1, None, 1)]
                or db.execute('PRAGMA table_info(persistent_worlds)').fetchall()
                    != [(0, 'world_id', 'TEXT', 1, None, 1), (1, 'owner_id', 'TEXT', 1, None, 0)]):
            raise ValueError('synthetic world registry is invalid')

    def claim(self, world_id):
        if not isinstance(world_id, str) or _WORLD.fullmatch(world_id) is None:
            raise ValueError('invalid synthetic world identity')
        try:
            with closing(self._open()) as db:
                db.execute('BEGIN IMMEDIATE')
                self._verify(db)
                try:
                    db.execute('INSERT INTO consumed_worlds (world_id) VALUES (?)', (world_id.lower(),))
                except sqlite3.IntegrityError:
                    raise ValueError('this Unreal world was already used by a team shelf demo') from None
                db.commit()
        except sqlite3.Error as exc:
            raise ValueError('synthetic world registry is unavailable or invalid') from exc

    def claim_persistent(self, world_id, owner_id, *, resume=False):
        """Bind once to a state identity; only that state may resume the world."""
        if (not isinstance(world_id, str) or _WORLD.fullmatch(world_id) is None
                or not isinstance(owner_id, str) or re.fullmatch(r'[a-f0-9]{64}', owner_id) is None
                or type(resume) is not bool):
            raise ValueError('valid simulated world and owner identities required')
        world_id = world_id.lower()
        try:
            with closing(self._open()) as db:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                self._verify(db)
                if resume:
                    if db.execute('SELECT owner_id FROM persistent_worlds WHERE world_id=?',
                                  (world_id,)).fetchone() != (owner_id,):
                        raise ValueError('world is not bound to this persistent state')
                else:
                    try:
                        db.execute('INSERT INTO consumed_worlds VALUES (?)', (world_id,))
                        db.execute('INSERT INTO persistent_worlds VALUES (?,?)', (world_id, owner_id))
                    except sqlite3.IntegrityError:
                        raise ValueError('simulated world or owner already used') from None
                db.commit()
        except sqlite3.Error as exc:
            raise ValueError('synthetic world registry is unavailable or invalid') from exc
