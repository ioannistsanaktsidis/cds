# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2016, 2017 CERN.
#
# Invenio is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""CDS Fixture Modules."""

from __future__ import absolute_import, print_function

import simplejson
import tarfile
import uuid
from os import listdir, makedirs
from os.path import basename, exists, isdir, join, splitext, dirname

from cds.modules.deposit.minters import catid_minter
import click
from cds.modules.deposit.api import Category, Video, Project
import pkg_resources
from cds.modules.ffmpeg import ff_probe
from invenio_sequencegenerator.api import Template
from cds_dojson.marc21 import marc21
from dojson.contrib.marc21.utils import create_record, split_blob
from flask import current_app
from flask.cli import with_appcontext
from invenio_db import db
from invenio_files_rest.models import (Bucket, FileInstance, Location,
                                       ObjectVersion, ObjectVersionTag)
from invenio_indexer.api import RecordIndexer
from invenio_pages import Page
from invenio_pidstore import current_pidstore
from invenio_records.api import Record
from invenio_records_files.api import Record as FileRecord
from invenio_records_files.models import RecordsBuckets


def _handle_source(source, temp):
    """Handle the input source.

    :param source: the file path
    :param temp: the temp directory to extract data
    :returns: the list of path files
    :rtype: list

    .. note::

        Returns always a list of file paths.

        Example output:

        ['/tmp/files/test_1.txt', '/tmp/files/test_2.txt']
    """
    if source.endswith('.tar.gz'):
        source = _untar(source, temp)

    return _get_files(source)


def _untar(source, temp):
    """Untar in location.

    :param source: the tar location
    :param temp: the temp directory to extract
    :returns: the path to the untar-ed
    :rtype: str

    .. note::

        Returns the *only* the extracted directory, if there is no directory
        in the tarball it will fail.

        Example of structure:

        files/
        files/test_1.txt
        files/test_2.txt
        files/test_3.txt
    """
    tar = tarfile.open(source)
    _untar_location = _dir(tar)
    tar.extractall(temp)
    tar.close()
    return join(temp, _untar_location)


def _dir(tar):
    """Return the enclosed directory.

    :param tar: the tar object to search for wrapper directory
    :returns: the name of the wrapper directory
    :rtype: str

    .. note::

        Example of structure:

        files/
        files/test_1.txt
        files/test_2.txt
        files/test_3.txt

        This structure will return ``files`` directory.
    """
    for tarinfo in tar:
        if splitext(tarinfo.name)[1] == '':
            return tarinfo.name


def _get_files(path):
    """Return the list of files.

    :param path: the path to files or directory
    :returns: a list of paths
    :rtype: list

    .. note::

        It will always return a list of path files.

        Example output:

        ['/tmp/files/test_1.txt', '/tmp/files/test_2.txt']
    """
    if isdir(path):
        return [join(path, name) for name in listdir(path)]
    return [path]


@click.group()
def fixtures():
    """Create demo records."""


@fixtures.command()
@click.option('--temp', '-t', default='/tmp')
@click.option('--source', '-s', default=False)
@with_appcontext
def cds(temp, source):
    """CDS demo records."""
    click.echo('Loading data it may take several minutes.')
    # pkg resources the demodata
    if not source:
        source = pkg_resources.resource_filename(
            'cds.modules.fixtures', 'data/records.xml'
        )
    files = _handle_source(source, temp)
    # Record indexer
    indexer = RecordIndexer()
    for f in files:
        with open(f) as source:
            # FIXME: Add some progress
            # with click.progressbar(data) as records:
            with db.session.begin_nested():
                for index, data in enumerate(split_blob(source.read()),
                                             start=1):
                    # create uuid
                    rec_uuid = uuid.uuid4()
                    # do translate
                    record = marc21.do(create_record(data))
                    # create PID
                    current_pidstore.minters['recid'](
                        rec_uuid, record
                    )
                    # create record
                    indexer.index(Record.create(record, id_=rec_uuid))
    db.session.commit()
    click.echo('DONE :)')


@fixtures.command()
@click.option('--temp', '-t', default='/tmp')
@click.option('--source', '-s', default=False)
@with_appcontext
def files(temp, source):
    """Demo files for testing.

    .. note::

        This files are *only* for testing.
    """
    click.echo('Loading files it may take several minutes.')
    if not source:
        source = pkg_resources.resource_filename(
            'cds.modules.fixtures', 'data/files.tar.gz'
        )

    files = _handle_source(source, temp)

    d = current_app.config['FIXTURES_FILES_LOCATION']
    if not exists(d):
        makedirs(d)

    # Clear data
    ObjectVersion.query.delete()
    Bucket.query.delete()
    FileInstance.query.delete()
    Location.query.delete()
    db.session.commit()

    # Create location
    loc = Location(name='local', uri=d, default=True)
    db.session.commit()

    # Record indexer
    indexer = RecordIndexer()
    for f in files:
        with open(join(source, f), 'rb') as fp:
            # Create bucket
            bucket = Bucket.create(loc)

            # The filename
            file_name = basename(f)

            # Create object version
            ObjectVersion.create(bucket, file_name, stream=fp)

            # Attach to dummy records
            rec_uuid = uuid.uuid4()
            record = {
                '_access': {
                    'read': ['orestis.melkonian@cern.ch', 'it-dep']
                },
                'dummy': True,
                'files': [
                    {
                        'uri': '/api/files/{0}/{1}'.format(
                            str(bucket.id), file_name),
                        'filename': file_name,
                        'bucket': str(bucket.id),
                        'local': True
                    }
                ]
            }

            # Create PID
            current_pidstore.minters['recid'](
                rec_uuid, record
            )

            # Create record
            record = FileRecord.create(record, id_=rec_uuid)

            # Index record
            indexer.index(record)

            # Create records' bucket
            RecordsBuckets.create(record=record.model, bucket=bucket)
    db.session.commit()
    click.echo('DONE :)')


@fixtures.command()
@click.option('--source', '-s', default=False)
@with_appcontext
def categories(source):
    """Load categories."""
    if not source:
        source = pkg_resources.resource_filename(
            'cds.modules.fixtures', 'data/categories.json'
        )

    with open(source, 'r') as fp:
        categories = simplejson.load(fp)

    # save in db
    to_index = []
    with db.session.begin_nested():
        for data in categories:
            cat_id = uuid.uuid4()
            catid_minter(cat_id, data)
            category = Category.create(data)
            to_index.append(category.id)
    db.session.commit()

    # index them
    indexer = RecordIndexer()
    for cat_id in to_index:
        indexer.index_by_id(cat_id)


@fixtures.command()
@with_appcontext
def sequence_generator():
    """Register CDS templates for sequence generation."""
    with db.session.begin_nested():
        Template.create(name='project-v1_0_0',
                        meta_template='{category}-{type}-{year}-{counter}',
                        start=1)
        Template.create(name='video-v1_0_0',
                        meta_template='{project-v1_0_0}-{counter}',
                        start=1)
    db.session.commit()


@fixtures.command()
@with_appcontext
def pages():
    """Register CDS static pages."""
    def page_data(page):
        return pkg_resources.resource_stream(
            'cds.modules.fixtures', join('data/pages', page)
        ).read().decode('utf8')

    pages = [
        Page(url='/about',
             title='About',
             description='About',
             content=page_data('about.html'),
             template_name='invenio_pages/dynamic.html'),
        Page(url='/contact',
             title='Contact',
             description='Contact',
             content=page_data('contact.html'),
             template_name='invenio_pages/dynamic.html'),
        Page(url='/faq',
             title='FAQ',
             description='FAQ',
             content=page_data('faq.html'),
             template_name='invenio_pages/dynamic.html'),
        Page(url='/feedback',
             title='Feedback',
             description='Feedback',
             content=page_data('feedback.html'),
             template_name='invenio_pages/dynamic.html'),
        Page(url='/help',
             title='Help',
             description='Help',
             content=page_data('help.html'),
             template_name='invenio_pages/dynamic.html'),
        Page(url='/terms',
             title='Terms of Use',
             description='Terms of Use',
             content=page_data('terms_of_use.html'),
             template_name='invenio_pages/dynamic.html')
    ]
    with db.session.begin_nested():
        Page.query.delete()
        db.session.add_all(pages)
    db.session.commit()
    click.echo('DONE :)')


@fixtures.command()
@click.option('--video', '-v', default=False)
@click.option('--frames', '-f', default=False)
@click.option('--temp', '-t', default='/tmp')
@click.option('--video-count', '-n', default=3)
@with_appcontext
def videos(video, frames, temp, video_count):
    """Load videos, frames and subformats."""
    if not video:
        video = join(dirname(__file__), '..', '..', '..',
                     'tests', 'data', 'test.mp4')
    video_duration = float(ff_probe(video, 'duration'))
    if not frames:
        frames = pkg_resources.resource_filename(
            'cds.modules.fixtures', 'data/frames.tar.gz'
        )

    frame_files = _handle_source(frames, temp)
    d = current_app.config['FIXTURES_FILES_LOCATION']
    if not exists(d):
        makedirs(d)

    project = Project.create(
        dict(
            date='2017-01-17',
            description=dict(value='desc'),
            title=dict(title='Project'),
            category='Category',
            type='Type')
    )
    project['_deposit']['owners'] = [1]
    project['_deposit']['created_by'] = 1
    project['videos'] = []

    # All deposits created
    deposits = [project]

    for video_index in range(video_count):
        with current_app.test_request_context():
            video_deposit = Video.create(
                dict(_project_id=str(project['_deposit']['id']),
                     contributors=[dict(name='contrib', role='Provider')],
                     copyright=dict(url='copyright'),
                     date='2017-01-16',
                     description=dict(value='desc'),
                     title=dict(title='Video'))
            )
        video_bucket = Bucket.get(video_deposit['_buckets']['deposit'])

        video_deposit['_deposit'].update(dict(
            owners=[1],
            created_by=1,
            extracted_metadata=dict(
                bit_rate='679886',
                duration=video_duration,
                size='5111048',
                avg_frame_rate='288000/12019',
                codec_name='h264',
                width=640,
                height=360,
                nb_frames='1440',
                display_aspect_ratio='16:9',
                color_range='tv',
            )
        ))

        with open(video, 'rb') as fp:
            master_obj = ObjectVersion.create(
                bucket=video_bucket,
                key='video{0}.mp4'.format(video_index),
                stream=fp)
        tags = [('preview', 'true'), ('bit_rate', '959963'),
                ('codec_name', 'h264'), ('duration', video_duration),
                ('nb_frames', '1557'), ('size', '10498667'),
                ('width', '1280'), ('height', '720'),
                ('display_aspect_ratio', '16:9'), ('avg_frame_rate', '25/1'),
                ('media_type', 'video'), ('context_type', 'master')]
        [ObjectVersionTag.create(master_obj, key, val) for key, val in tags]

        number_of_frames = len(frame_files)
        frame_files.sort()
        frame_files.sort(key=len)
        for i, f in enumerate(frame_files):
            with open(join(frames, f), 'rb') as fp:
                # The filename
                file_name = basename(f)

                obj = ObjectVersion.create(
                    bucket=video_bucket,
                    key=file_name,
                    stream=fp)
                ObjectVersionTag.create(obj, 'media_type', 'image')
                ObjectVersionTag.create(obj, 'context_type', 'frame')
                ObjectVersionTag.create(obj, 'master', master_obj.version_id)
                ObjectVersionTag.create(
                    obj, 'timestamp',
                    (float(i) / number_of_frames) * video_duration)

        for quality in ['360p', '480p', '720p']:
            with open(video, 'rb') as fp:
                obj = ObjectVersion.create(
                    bucket=video_bucket,
                    key='video{0}[{1}].mp4'.format(video_index, quality),
                    stream=fp)
            ObjectVersionTag.create(obj, 'media_type', 'video')
            ObjectVersionTag.create(obj, 'context_type', 'subformat')
            ObjectVersionTag.create(obj, 'master', master_obj.version_id)
            ObjectVersionTag.create(obj, 'preset_quality', quality)

        deposits.append(video_deposit.commit())
    project.commit()
    with current_app.test_request_context():
        project.publish()
        indexer = RecordIndexer()
        # index all published records
        for deposit in deposits:
            _, record = deposit.fetch_published()
            indexer.index(record)
    db.session.commit()
    click.echo('DONE :)')
