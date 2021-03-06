# coding=utf-8
#
# This file is part of Medusa.
#
# Medusa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Medusa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Medusa. If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import os
import shutil
import socket
import stat

from medusa.clients import torrent

import requests

import shutil_custom

from unrar2 import RarFile
from unrar2.rar_exceptions import (ArchiveHeaderBroken, FileOpenError, IncorrectRARPassword, InvalidRARArchive,
                                   InvalidRARArchiveUsage)
from . import app, db, failed_processor, helpers, logger, notifiers, post_processor
from .helper.common import is_sync_file
from .helper.exceptions import EpisodePostProcessingFailedException, FailedPostProcessingFailedException, ex
from .name_parser.parser import InvalidNameException, InvalidShowException, NameParser
from .subtitles import accept_any, accept_unknown, get_embedded_subtitles

shutil.copyfile = shutil_custom.copyfile_custom


class ProcessResult(object):

    IGNORED_FOLDERS = ('.AppleDouble', '.@__thumb', '@eaDir')

    def __init__(self, path, process_method=None):

        self._output = []
        self.directory = path
        self.process_method = process_method
        self.resource_name = None
        self.result = True
        self.succeeded = True
        self.missedfiles = []
        self.allowed_extensions = app.ALLOWED_EXTENSIONS.split(',')
        self.postponed_no_subs = False

    @property
    def directory(self):
        """Return the root directory we are going to process."""
        return getattr(self, '_directory')

    @directory.setter
    def directory(self, path):
        directory = None
        if os.path.isdir(path):
            self._log('Processing path: {0}'.format(path), logger.DEBUG)
            directory = os.path.realpath(path)

        # If the client and the application are not on the same machine,
        # translate the directory into a network directory
        elif all([app.TV_DOWNLOAD_DIR, os.path.isdir(app.TV_DOWNLOAD_DIR),
                  os.path.normpath(path) == os.path.normpath(app.TV_DOWNLOAD_DIR)]):
            directory = os.path.join(
                app.TV_DOWNLOAD_DIR,
                os.path.abspath(path).split(os.path.sep)[-1]
            )
            self._log('Trying to use folder: {0}'.format(directory),
                      logger.DEBUG)
        else:
            self._log("Unable to figure out what folder to process."
                      " If your download client and Medusa aren't on the same"
                      " machine, make sure to fill out the Post Processing Dir"
                      " field in the config.", logger.WARNING)
        setattr(self, '_directory', directory)

    @property
    def paths(self):
        """Return the paths we are going to try to process."""
        if self.directory:
            yield self.directory
            if self.resource_name:
                return
            for root, dirs, files in os.walk(self.directory):
                del files  # unused variable
                for folder in dirs:
                    path = os.path.join(root, folder)
                    yield path
                break

    @property
    def video_files(self):
        return getattr(self, '_video_files', [])

    @video_files.setter
    def video_files(self, value):
        setattr(self, '_video_files', value)

    @property
    def output(self):
        return '\n'.join(self._output)

    def _log(self, message, level=logger.INFO):
        logger.log(message, level)
        self._output.append(message)

    def process(self, resource_name=None, force=False, is_priority=None, delete_on=False, failed=False,
                proc_type='auto', ignore_subs=False):
        """
        Scan through the files in the root directory and process whatever media files are found.

        :param resource_name: The resource that will be processed directly
        :param force: True to postprocess already postprocessed files
        :param is_priority: Boolean for whether or not is a priority download
        :param delete_on: Boolean for whether or not it should delete files
        :param failed: Boolean for whether or not the download failed
        :param proc_type: Type of postprocessing auto or manual
        :param ignore_subs: True to ignore setting 'postpone if no subs'
        """
        if not self.directory:
            return self.output

        if resource_name:
            self.resource_name = resource_name

        if app.POSTPONE_IF_NO_SUBS:
            self._log("Feature 'postpone post-processing if no subtitle available' is enabled.")

        for path in self.paths:

            if not self.should_process(path, failed):
                continue

            self.result = True

            for dir_path, filelist in self._get_files(path):

                sync_files = (filename
                              for filename in filelist
                              if is_sync_file(filename))
                # Don't process files if they are still being synced
                postpone = app.POSTPONE_IF_SYNC_FILES and any(sync_files)

                if not postpone:

                    self._log('Processing folder: {0}'.format(dir_path), logger.DEBUG)

                    self.prepare_files(dir_path, filelist, force)

                    self.process_files(dir_path, force=force, is_priority=is_priority,
                                       ignore_subs=ignore_subs)

                    # Always delete files if they are being moved or if it's explicitly wanted
                    if not self.process_method == 'move' or (proc_type == 'manual' and not delete_on):
                        continue

                    self.delete_folder(os.path.join(dir_path, '@eaDir'))
                    if self.unwanted_files:
                        self.delete_files(dir_path, self.unwanted_files)

                    if all([not app.NO_DELETE or proc_type == 'manual', self.process_method == 'move',
                            os.path.normpath(dir_path) != os.path.normpath(app.TV_DOWNLOAD_DIR)]):

                        if self.delete_folder(dir_path, check_empty=True):
                            self._log('Deleted folder: {0}'.format(dir_path), logger.DEBUG)

                else:
                    self._log('Found temporary sync files in folder: {0}'.format(dir_path))
                    self._log('Skipping post processing for folder: {0}'.format(dir_path))
                    self.missedfiles.append('{0}: Sync files found'.format(dir_path))

        if self.succeeded:
            self._log('Successfully processed.')

            # Clean Kodi library
            if app.KODI_LIBRARY_CLEAN_PENDING and notifiers.kodi_notifier.clean_library():
                app.KODI_LIBRARY_CLEAN_PENDING = False

            if self.missedfiles:
                self._log('I did encounter some unprocessable items: ')
                for missedfile in self.missedfiles:
                    self._log('{0}'.format(missedfile))
        else:
            self._log('Problem(s) during processing, failed for the following files/folders: ', logger.WARNING)
            for missedfile in self.missedfiles:
                self._log('{0}'.format(missedfile), logger.WARNING)

        if app.USE_TORRENTS and app.PROCESS_METHOD in ('hardlink', 'symlink') and app.TORRENT_SEED_LOCATION:
            to_remove_hashes = app.RECENTLY_POSTPROCESSED.items()
            for info_hash, release_names in to_remove_hashes:
                if self.move_torrent_seeding_folder(info_hash, release_names):
                    app.RECENTLY_POSTPROCESSED.pop(info_hash)

        return self.output

    def should_process(self, path, failed=False):
        """
        Determine if a directory should be processed.

        :param path: Path we want to verify
        :param failed: (optional) Mark the directory as failed
        :return: True if the directory is valid for processing, otherwise False
        :rtype: Boolean
        """
        folder = os.path.basename(path)
        if folder in self.IGNORED_FOLDERS:
            return False

        if folder.startswith('_FAILED_'):
            self._log('The directory name indicates it failed to extract.', logger.DEBUG)
            failed = True
        elif folder.startswith('_UNDERSIZED_'):
            self._log('The directory name indicates that it was previously rejected for being undersized.',
                      logger.DEBUG)
            failed = True
        elif folder.upper().startswith('_UNPACK'):
            self._log('The directory name indicates that this release is in the process of being unpacked.',
                      logger.DEBUG)
            self.missedfiles.append('{0}: Being unpacked'.format(folder))
            return False

        if failed:
            self.process_failed(path)
            self.missedfiles.append('{0}: Failed download'.format(folder))
            return False

        if helpers.is_hidden_folder(path):
            self._log('Ignoring hidden folder: {0}'.format(folder), logger.DEBUG)
            self.missedfiles.append('{0}: Hidden folder'.format(folder))
            return False

        for root, dirs, files in os.walk(path):
            for each_file in files:
                if helpers.is_media_file(each_file) or helpers.is_rar_file(each_file):
                    return True
            del root  # unused variable
            del dirs  # unused variable

        self._log('No processable items found in folder: {0}'.format(path), logger.DEBUG)
        return False

    def _get_files(self, path):
        """Return the path to a folder and its contents as a tuple."""
        # If resource_name is a file and not an NZB, process it directly
        if self.resource_name and (not self.resource_name.endswith('.nzb') and
                                   os.path.isfile(os.path.join(path, self.resource_name))):
            yield path, [self.resource_name]
        else:
            topdown = True if self.directory == path else False
            for root, dirs, files in os.walk(path, topdown=topdown):
                if files:
                    yield root, files
                if topdown:
                    break
                del dirs  # unused variable

    def prepare_files(self, path, files, force=False):
        """Prepare files for post-processing."""
        video_files = []
        rar_files = []
        for each_file in files:
            if helpers.is_media_file(each_file):
                video_files.append(each_file)
            elif helpers.is_rar_file(each_file):
                rar_files.append(each_file)

        rar_content = []
        video_in_rar = []
        if rar_files:
            rar_content = self.unrar(path, rar_files, force)
            files.extend(rar_content)
            video_in_rar = [each_file for each_file in rar_content if helpers.is_media_file(each_file)]
            video_files.extend(video_in_rar)

        self._log('Post-processing files: {0}'.format(files), logger.DEBUG)
        self._log('Post-processing video files: {0}'.format(video_files), logger.DEBUG)

        if rar_content:
            self._log('Post-processing rar content: {0}'.format(rar_content), logger.DEBUG)
            self._log('Post-processing video in rar: {0}'.format(video_in_rar), logger.DEBUG)

        unwanted_files = [filename
                          for filename in files
                          if filename not in video_files and
                          helpers.get_extension(filename) not in
                          self.allowed_extensions]
        if unwanted_files:
            self._log('Found unwanted files: {0}'.format(unwanted_files), logger.DEBUG)

        self.video_files = video_files
        self.rar_content = rar_content
        self.video_in_rar = video_in_rar
        self.unwanted_files = unwanted_files

    def process_files(self, path, force=False, is_priority=None, ignore_subs=False):
        """Post-process and delete the files in a given path."""
        # TODO: Replace this with something that works for multiple video files
        if self.resource_name and len(self.video_files) > 1:
            self.resource_name = None

        # Don't Link media when the media is extracted from a rar in the same path
        if self.process_method in ('hardlink', 'symlink') and self.video_in_rar:
            self.process_media(path, self.video_in_rar, force, is_priority, ignore_subs)

            self.process_media(path, set(self.video_files) - set(self.video_in_rar), force,
                               is_priority, ignore_subs)

            if not self.postponed_no_subs:
                self.delete_files(path, self.rar_content)
            else:
                self.postponed_no_subs = False

        elif app.DELRARCONTENTS and self.video_in_rar:
            self.process_media(path, self.video_in_rar, force, is_priority, ignore_subs)

            self.process_media(path, set(self.video_files) - set(self.video_in_rar),
                               force, is_priority, ignore_subs)

            if not self.postponed_no_subs:
                self.delete_files(path, self.rar_content, force=True)
            else:
                self.postponed_no_subs = False

        else:
            self.process_media(path, self.video_files, force, is_priority, ignore_subs)
            self.postponed_no_subs = False

    @staticmethod
    def delete_folder(folder, check_empty=True):
        """
        Remove a folder from the filesystem.

        :param folder: Path to folder to remove
        :param check_empty: Boolean, check if the folder is empty before removing it, defaults to True
        :return: True on success, False on failure
        """
        # check if it's a folder
        if not os.path.isdir(folder):
            return False

        # check if it isn't TV_DOWNLOAD_DIR
        if app.TV_DOWNLOAD_DIR:
            if helpers.real_path(folder) == helpers.real_path(app.TV_DOWNLOAD_DIR):
                return False

        # check if it's empty folder when wanted checked
        if check_empty:
            check_files = os.listdir(folder)
            if check_files:
                logger.log('Not deleting folder {0} found the following files: {1}'.format
                           (folder, check_files), logger.INFO)
                return False

            try:
                logger.log("Deleting folder (if it's empty): {0}".format(folder))
                os.rmdir(folder)
            except (OSError, IOError) as error:
                logger.log('Unable to delete folder: {0}: {1}'.format(folder, ex(error)), logger.WARNING)
                return False
        else:
            try:
                logger.log('Deleting folder: {0}'.format(folder))
                shutil.rmtree(folder)
            except (OSError, IOError) as error:
                logger.log('Unable to delete folder: {0}: {1}'.format(folder, ex(error)), logger.WARNING)
                return False

        return True

    def delete_files(self, path, files, force=False):
        """
        Remove files from filesystem.

        :param path: path to process
        :param files: files we want to delete
        :param result: Processor results
        :param force: Boolean, force deletion, defaults to false
        """
        if not files:
            return

        if not self.result and force:
            self._log('Forcing deletion of files, even though last result was not successful.', logger.DEBUG)
        elif not self.result:
            return

        # Delete all file not needed
        for cur_file in files:

            cur_file_path = os.path.join(path, cur_file)

            if not os.path.isfile(cur_file_path):
                continue  # Prevent error when a notwantedfiles is an associated files

            self._log('Deleting file: {0}'.format(cur_file), logger.DEBUG)

            # check first the read-only attribute
            file_attribute = os.stat(cur_file_path)[0]
            if not file_attribute & stat.S_IWRITE:
                # File is read-only, so make it writeable
                self._log('Changing read-only flag for file: {0}'.format(cur_file), logger.DEBUG)
                try:
                    os.chmod(cur_file_path, stat.S_IWRITE)
                except OSError as error:
                    self._log('Cannot change permissions of {0}: {1}'.format(cur_file_path, ex(error)), logger.DEBUG)
            try:
                os.remove(cur_file_path)
            except OSError as error:
                self._log('Unable to delete file {0}: {1}'.format(cur_file, ex(error)), logger.DEBUG)

    def unrar(self, path, rar_files, force=False):
        """
        Extract RAR files.

        :param path: Path to look for files in
        :param rarFiles: Names of RAR files
        :param force: process currently processing items
        :param result: Previous results
        :return: List of unpacked file names
        """
        unpacked_files = []

        if app.UNPACK and rar_files:

            self._log('Packed files detected: {0}'.format(rar_files), logger.DEBUG)

            for archive in rar_files:

                self._log('Unpacking archive: {0}'.format(archive), logger.DEBUG)

                failure = None
                try:
                    rar_handle = RarFile(os.path.join(path, archive))

                    # Skip extraction if any file in archive has previously been extracted
                    skip_file = False
                    for file_in_archive in [os.path.basename(each.filename)
                                            for each in rar_handle.infolist()
                                            if not each.isdir]:
                        if not force and self.already_postprocessed(file_in_archive):
                            self._log('Archive file already post-processed, extraction skipped: {0}'.format
                                      (file_in_archive), logger.DEBUG)
                            skip_file = True
                            break

                        if app.POSTPONE_IF_NO_SUBS and os.path.isfile(os.path.join(path, file_in_archive)):
                            self._log('Archive file already extracted, extraction skipped: {0}'.format
                                      (file_in_archive), logger.DEBUG)
                            skip_file = True
                            # We need to return the media file inside the rar so we can
                            # move it when the method is hardlink/symlink
                            unpacked_files.append(file_in_archive)
                            break

                    if skip_file:
                        continue

                    rar_handle.extract(path=path, withSubpath=False, overwrite=False)
                    for each in rar_handle.infolist():
                        if not each.isdir:
                            basename = os.path.basename(each.filename)
                            if basename not in unpacked_files:
                                unpacked_files.append(basename)
                    del rar_handle

                except ArchiveHeaderBroken:
                    failure = ('Archive Header Broken', 'Unpacking failed because the Archive Header is Broken')
                except IncorrectRARPassword:
                    failure = ('Incorrect RAR Password', 'Unpacking failed because of an Incorrect Rar Password')
                except FileOpenError:
                    failure = ('File Open Error, check the parent folder and destination file permissions.',
                               'Unpacking failed with a File Open Error (file permissions?)')
                except InvalidRARArchiveUsage:
                    failure = ('Invalid Rar Archive Usage', 'Unpacking Failed with Invalid Rar Archive Usage')
                except InvalidRARArchive:
                    failure = ('Invalid Rar Archive', 'Unpacking Failed with an Invalid Rar Archive Error')
                except Exception as error:
                    failure = (ex(error), 'Unpacking failed for an unknown reason')

                if failure is not None:
                    self._log('Failed Unrar archive {0}: {1}'.format(archive, failure[0]), logger.WARNING)
                    self.missedfiles.append('{0}: Unpacking failed: {1}'.format(archive, failure[1]))
                    self.result = False
                    continue

            self._log('Unrar content: {0}'.format(unpacked_files), logger.DEBUG)

        return unpacked_files

    def already_postprocessed(self, video_file):
        """
        Check if we already post processed a file.

        :param video_file: File name
        :param result: True if file is already postprocessed
        :return:
        """
        main_db_con = db.DBConnection()
        history_result = main_db_con.select(
            'SELECT * FROM history '
            "WHERE action LIKE '%04' "
            'AND resource LIKE ?',
            ['%' + video_file])

        if history_result:
            self._log("You're trying to post-process a file that has already "
                      "been processed, skipping: {0}".format(video_file), logger.DEBUG)
            return True

    def process_media(self, path, video_files, force=False, is_priority=None, ignore_subs=False):
        """
        Postprocess media files.

        :param processPath: Path to postprocess in
        :param videoFiles: Filenames to look for and postprocess
        :param force: Postprocess currently postprocessing file
        :param is_priority: Boolean, is this a priority download
        :param result: Previous results
        :param ignore_subs: True to ignore setting 'postpone if no subs'
        """
        processor = None
        for video_file in video_files:
            file_path = os.path.join(path, video_file)

            if not force and self.already_postprocessed(video_file):
                self._log('Skipping already processed file: {0}'.format(video_file), logger.DEBUG)
                self._log('Skipping already processed directory: {0}'.format(path), logger.DEBUG)
                continue

            try:
                processor = post_processor.PostProcessor(file_path, self.resource_name,
                                                         self.process_method, is_priority)

                if app.POSTPONE_IF_NO_SUBS:
                    if not ignore_subs:
                        if self.subtitles_enabled(file_path, self.resource_name):
                            embedded_subs = set() if app.IGNORE_EMBEDDED_SUBS else get_embedded_subtitles(file_path)

                            # We want to ignore embedded subtitles and video has at least one
                            if accept_unknown(embedded_subs):
                                self._log("Found embedded unknown subtitles and we don't want to ignore them. "
                                          "Continuing the post-processing of this file: {0}".format(video_file))
                            elif accept_any(embedded_subs):
                                self._log('Found wanted embedded subtitles. '
                                          'Continuing the post-processing of this file: {0}'.format(video_file))
                            else:
                                associated_subs = processor.list_associated_files(file_path, subtitles_only=True)
                                if not associated_subs:
                                    self._log('No subtitles associated. Postponing the post-process of this file: '
                                              '{0}'.format(video_file), logger.DEBUG)
                                    self.postponed_no_subs = True
                                    continue
                                else:
                                    self._log('Found subtitles associated. '
                                              'Continuing the post-process of this file: {0}'.format(video_file))
                        else:
                            self._log('Subtitles disabled for this show. '
                                      'Continuing the post-process of this file: {0}'.format(video_file))
                    else:
                        self._log('Subtitles check was disabled for this episode in manual post-processing. '
                                  'Continuing the post-process of this file: {0}'.format(video_file))

                self.result = processor.process()
                process_fail_message = ''
            except EpisodePostProcessingFailedException as error:
                self.result = False
                process_fail_message = ex(error)

            if processor:
                self._output.append(processor.log)

            if self.result:
                self._log('Processing succeeded for {0}'.format(file_path))
            else:
                self._log('Processing failed for {0}: {1}'.format(file_path, process_fail_message), logger.WARNING)
                self.missedfiles.append('{0}: Processing failed: {1}'.format(file_path, process_fail_message))
                self.succeeded = False

    def process_failed(self, path):
        """Process a download that did not complete correctly."""
        if app.USE_FAILED_DOWNLOADS:
            processor = None

            try:
                processor = failed_processor.FailedProcessor(path, self.resource_name)
                self.result = processor.process()
                process_fail_message = ''
            except FailedPostProcessingFailedException as error:
                self.result = False
                process_fail_message = ex(error)

            if processor:
                self._output.append(processor.log)

            if app.DELETE_FAILED and self.result:
                if self.delete_folder(path, check_empty=False):
                    self._log('Deleted folder: {0}'.format(path), logger.DEBUG)

            if self.result:
                self._log('Failed Download Processing succeeded: {0}, {1}'.format
                          (self.resource_name, path))
            else:
                self._log('Failed Download Processing failed: {0}, {1}: {2}'.format
                          (self.resource_name, path, process_fail_message), logger.WARNING)

    @staticmethod
    def subtitles_enabled(*args):
        """Try to parse names to a show and check whether the show has subtitles enabled.

        :param args:
        :return:
        :rtype: bool
        """
        for name in args:
            if not name:
                continue

            try:
                parse_result = NameParser().parse(name, cache_result=True)
                if parse_result.show.indexerid:
                    main_db_con = db.DBConnection()
                    sql_results = main_db_con.select("SELECT subtitles FROM tv_shows WHERE indexer_id = ? LIMIT 1",
                                                     [parse_result.show.indexerid])
                    return bool(sql_results[0][b'subtitles']) if sql_results else False

                logger.log('Empty indexer ID for: {name}'.format(name=name), logger.WARNING)
            except (InvalidNameException, InvalidShowException):
                logger.log('Not enough information to parse filename into a valid show. Consider adding scene '
                           'exceptions or improve naming for: {name}'.format(name=name), logger.WARNING)
        return False

    @staticmethod
    def move_torrent_seeding_folder(info_hash, release_names):
        """Move torrent to a given seeding folder after PP."""
        if not os.path.isdir(app.TORRENT_SEED_LOCATION):
            logger.log('Not possible to move torrent after Post-Processor because seed location is invalid',
                       logger.WARNING)
            return False
        else:
            if release_names:
                # Log 'release' or 'releases'
                s = 's' if len(release_names) > 1 else ''
                release_names = ', '.join(release_names)
            else:
                s = ''
                release_names = 'N/A'
            logger.log('Trying to move torrent after Post-Processor', logger.DEBUG)
            torrent_moved = False
            client = torrent.get_client_class(app.TORRENT_METHOD)()
            try:
                torrent_moved = client.move_torrent(info_hash)
            except (requests.exceptions.RequestException, socket.gaierror) as e:
                logger.log("Couldn't connect to client to move torrent for release{s} '{release}' with hash: {hash} "
                           "to: '{path}'. Error: {error}".format
                           (release=release_names, hash=info_hash, error=e.message, path=app.TORRENT_SEED_LOCATION, s=s),
                           logger.WARNING)
                return False
            except AttributeError:
                logger.log("Your client doesn't support moving torrents to new location", logger.WARNING)
                return True
            if torrent_moved:
                logger.log("Moved torrent for release{s} '{release}' with hash: {hash} to: '{path}'".format
                           (release=release_names, hash=info_hash, path=app.TORRENT_SEED_LOCATION, s=s),
                           logger.DEBUG)
                return True
            else:
                logger.log("Could not move torrent for release{s} '{release}' with hash: {hash} to: '{path}'. "
                           "Please check logs.".format(release=release_names, hash=info_hash, s=s,
                                                       path=app.TORRENT_SEED_LOCATION), logger.WARNING)
                return False
