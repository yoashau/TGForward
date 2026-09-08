#  Pyrofork - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-present Dan <https://github.com/delivrance>
#  Copyright (C) 2022-present Mayuri-Chan <https://github.com/Mayuri-Chan>
#
#  This file is part of Pyrofork.
#
#  Pyrofork is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Pyrofork is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Pyrofork.  If not, see <http://www.gnu.org/licenses/>.

"""Pyrofork 2.3.69 get_file 修订：媒体会话按 client/DC 缓存，保留原 CDN 校验。

下方 get_file 基于该版本 LGPL-3.0-or-later 实现；仅改会话获取、错误传播和限流阈值。
会话存入原生 media_sessions，由 Client.terminate 统一关闭并清空。
"""

import functools
import inspect
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from hashlib import sha256

import pyrogram
from pyrogram import raw, utils
from pyrogram.crypto import aes
from pyrogram.errors import CDNFileHashMismatch, VolumeLocNotFound
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.session import Auth, Session

logger = logging.getLogger(__name__)


async def media_session(client, dc_id, *, cdn=False):
    key = ("tf-download", dc_id, cdn)
    # 只缓存完全初始化的会话；同一 client 并发请求不会重复授权。
    async with client.media_sessions_lock:
        if key in client.media_sessions:
            return client.media_sessions[key]
        home_dc = await client.storage.dc_id()
        test_mode = await client.storage.test_mode()
        exported = None
        if dc_id != home_dc and not cdn:
            exported = await client.invoke(
                raw.functions.auth.ExportAuthorization(dc_id=dc_id), sleep_threshold=0
            )
        auth_key = (
            await client.storage.auth_key()
            if dc_id == home_dc and not cdn
            else await Auth(client, dc_id, test_mode).create()
        )
        session = Session(client, dc_id, auth_key, test_mode, is_media=True, is_cdn=cdn)
        try:
            await session.start()
            if exported is not None:
                await session.invoke(
                    raw.functions.auth.ImportAuthorization(id=exported.id, bytes=exported.bytes),
                    sleep_threshold=0,
                )
        except BaseException:
            with suppress(Exception):
                await session.stop()
            raise
        client.media_sessions[key] = session
        logger.info("媒体会话已缓存 account=%s dc=%s cdn=%s", client.name, dc_id, cdn)
        return session


async def get_file(
    self,
    file_id: FileId,
    file_size: int = 0,
    limit: int = 0,
    offset: int = 0,
    progress: Callable = None,
    progress_args: tuple = (),
) -> AsyncGenerator[bytes, None]:
    async with self.get_file_semaphore:
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )

            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                photo_id=file_id.media_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )

        current = 0
        total = abs(limit) or (1 << 31) - 1
        chunk_size = 1024 * 1024
        offset_bytes = abs(offset) * chunk_size

        dc_id = file_id.dc_id

        session = await media_session(self, dc_id)

        try:
            r = await session.invoke(
                raw.functions.upload.GetFile(
                    location=location, offset=offset_bytes, limit=chunk_size
                ),
                sleep_threshold=0,
            )

            if isinstance(r, raw.types.upload.File):
                while True:
                    chunk = r.bytes

                    yield chunk

                    current += 1
                    offset_bytes += chunk_size

                    if progress:
                        func = functools.partial(
                            progress,
                            min(offset_bytes, file_size) if file_size != 0 else offset_bytes,
                            file_size,
                            *progress_args,
                        )

                        if inspect.iscoroutinefunction(progress):
                            await func()
                        else:
                            await self.loop.run_in_executor(self.executor, func)

                    if len(chunk) < chunk_size or current >= total:
                        break

                    r = await session.invoke(
                        raw.functions.upload.GetFile(
                            location=location, offset=offset_bytes, limit=chunk_size
                        ),
                        sleep_threshold=0,
                    )

            elif isinstance(r, raw.types.upload.FileCdnRedirect):
                cdn_session = await media_session(self, r.dc_id, cdn=True)

                try:
                    while True:
                        r2 = await cdn_session.invoke(
                            raw.functions.upload.GetCdnFile(
                                file_token=r.file_token, offset=offset_bytes, limit=chunk_size
                            ),
                            sleep_threshold=0,
                        )

                        if isinstance(r2, raw.types.upload.CdnFileReuploadNeeded):
                            try:
                                await session.invoke(
                                    raw.functions.upload.ReuploadCdnFile(
                                        file_token=r.file_token, request_token=r2.request_token
                                    ),
                                    sleep_threshold=0,
                                )
                            except VolumeLocNotFound:
                                raise
                            else:
                                continue

                        chunk = r2.bytes

                        # https://core.telegram.org/cdn#decrypting-files
                        decrypted_chunk = aes.ctr256_decrypt(
                            chunk,
                            r.encryption_key,
                            bytearray(
                                r.encryption_iv[:-4] + (offset_bytes // 16).to_bytes(4, "big")
                            ),
                        )

                        hashes = await session.invoke(
                            raw.functions.upload.GetCdnFileHashes(
                                file_token=r.file_token, offset=offset_bytes
                            ),
                            sleep_threshold=0,
                        )

                        # https://core.telegram.org/cdn#verifying-files
                        for i, h in enumerate(hashes):
                            cdn_chunk = decrypted_chunk[h.limit * i : h.limit * (i + 1)]
                            CDNFileHashMismatch.check(
                                h.hash == sha256(cdn_chunk).digest(),
                                "h.hash == sha256(cdn_chunk).digest()",
                            )

                        yield decrypted_chunk

                        current += 1
                        offset_bytes += chunk_size

                        if progress:
                            func = functools.partial(
                                progress,
                                min(offset_bytes, file_size) if file_size != 0 else offset_bytes,
                                file_size,
                                *progress_args,
                            )

                            if inspect.iscoroutinefunction(progress):
                                await func()
                            else:
                                await self.loop.run_in_executor(self.executor, func)

                        if len(chunk) < chunk_size or current >= total:
                            break
                except Exception as e:
                    raise e
        except pyrogram.StopTransmission:
            raise
        except pyrogram.errors.FloodWait:
            raise
        except Exception:
            # 不吞掉分块错误，否则 handle_download 会把残缺文件当作成功。
            raise
