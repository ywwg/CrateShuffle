#!/usr/bin/python3
# -*- coding: utf-8 -*-

import argparse
import logging
import os, os.path
import re
import shutil
import subprocess
import sys
import threading

import mutagen
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, COMM, POPM
from mutagen.oggvorbis import OggVorbis

logging.basicConfig(level=logging.DEBUG)

THREADPOOL_SIZE = 3

#http://www.faqts.com/knowledge_base/view.phtml/aid/2682
class GlobDirectoryWalker:
  """a forward iterator that traverses a directory tree"""

  def __init__(self, directory, pattern="*"):
    self.stack = [directory]
    self.pattern = pattern
    self.files = []
    self.index = 0

  def __getitem__(self, index):
    import fnmatch
    while 1:
      try:
        file = self.files[self.index]
        self.index = self.index + 1
      except IndexError:
        # pop next directory from stack
        try:
          while True:
            self.directory = self.stack.pop()
            self.index = 0
            self.files = os.listdir(self.directory) #loops if we have a problem listing the directory
            break #but if it works we break
        except OSError as e:
          continue #evil... but it works
      else:
        # got a filename
        fullname = os.path.join(self.directory, file)
        if os.path.isdir(fullname) and not os.path.islink(fullname):
          self.stack.append(fullname)
        if fnmatch.fnmatch(file, self.pattern):
          return fullname

def get_audiofile(filename):
  ext = filename.split('.')[-1].lower()
  if ext == 'mp3':
    return ID3(filename)
  elif ext == 'flac':
    return FLAC(filename)
  elif ext == 'ogg':
    return OggVorbis(filename)
  logging.warning('neither mp3 nor flac nor ogg, skipping')
  return None


def get_genre(audiofile):
  genre = 'Unknown'
  if audiofile is None:
    return genre
  for k in ('genre', 'TCON'):
    if k not in audiofile:
      continue
    if type(audiofile[k]) != list:
      continue
    genre = audiofile[k][0].title().strip()
    # TODO: replace other path-unsafe characters
    genre = genre.replace('/',',')
    break
  # If multiple genres listed, just take the first.
  return genre.split(',')[0].strip()


def get_level(audiofile):
  if audiofile is None:
    return 'l0'
  comment = 'No Comment'
  for k in ('description', 'COMM::Pur', 'TCON'):
    if k not in audiofile:
      continue
    try:
      comment = audiofile[k][0]
      break
    except:
      pass

  # Incredibly Owen-specific.  Until I map my comments to ratings, I have to do this
  level = 'Unknown'
  level_regex = re.compile(r'^l\d$')
  multilevel_regex = re.compile(r'^l\d-l\d$')
  tokens = comment.split(',')
  for t in tokens:
    t = t.strip()
    m = level_regex.match(t)
    m2 = multilevel_regex.match(t)
    if m:
      return m.group(0)
    elif m2:
      return m2.group(0)
  return level


#def rating_from_level(level):
#  level_regex = re.compile(r'^l(\d)$')
#  multilevel_regex = re.compile(r'^l\d-l(\d)$')
#  multimatch = multilevel_regex.match(level)
#  if multimatch:
#    return int(multimatch.group(1))
#  match = level_regex.match(level)
#  if match:
#    return int(match.group(1))
#  return 0


#def set_rating(audiofile, stars):
#  # loooool
#  scaled_rating = 0
#  if stars == 1:
#    scaled_rating = 1
#  elif stars == 2:
#    scaled_rating = 64
#  elif stars == 3:
#    scaled_rating = 128
#  elif stars == 4:
#    scaled_rating = 196
#  elif stars == 5:
#    scaled_rating = 255
#
#  rating_email = 'Windows Media Player 9 Series'
#
#  popm = POPM(rating_email, scaled_rating, 0)
#  audiofile.add(popm)
#  audiofile.save()


def get_destination_subfolder(audiofile):
  """Using the id3 tags, construct a subfolder name.

  This should be generalized so that people can configure their own path structures."""

  genre = get_genre(audiofile)
  level = get_level(audiofile)

  return os.path.join(genre, level)


def fix_ffmpeg_tag(tag_list):
  """Fix ffmpeg's bad tag values.

  Sometimes a tag will be of the form "foobar;foobar".  Detect this case and return the corrected
  text.
  """

  tag = tag_list[0]
  mid = len(tag)//2
  if tag[:mid] == tag[mid+1:]:
    return [tag[:mid],]
  return tag_list


def transcode_file(pool_sema, source, dest, dryrun=False):
  """Take the source file and transcode it to the destination file name.

  dest name should already end in .mp3.
  """

  with pool_sema:
    assert(dest.split('.')[-1] == 'mp3')

    # XXX: OGG tags don't seem to be copied
    cmd = ['ffmpeg', '-i', source, '-q:a', '1', dest, '-y', '-loglevel', '16']
    logging.info('%s -> TRANSCODE -> %s' % (os.path.basename(source), dest))
    if dryrun:
      return

    # NOTE: as of python 3.5 this is called .run().
    result = subprocess.call(cmd)
    if result != 0:
      logging.warning('Error transcoding!')
      return

    # ffmpeg messes up the tags for some reason :(
    audiofile = ID3(dest)
    for key in ('TIT2', 'TALB', 'TPE1', 'TPE2', 'TPE3', 'TPE4', 'COMM',
                'COMM:Pur', 'TXXX:comment', 'TCON'):
      if key not in audiofile:
        continue
      audiofile[key].text = fix_ffmpeg_tag(audiofile[key].text)

      # Convert to a more universal version of comment storage
      if key == 'TXXX:comment':
        audiofile.add(COMM(encoding=0, text=audiofile[key].text[0]))
    audiofile.save()


def main(library, destination, transcode=True, overwrite=False, dryrun=False):
  logging.info('rearranging the dir %s into %s' % (library, destination))

  pool_sema = threading.BoundedSemaphore(THREADPOOL_SIZE)

  threads = []

  for f in GlobDirectoryWalker(library, "*"):
    audiofile = get_audiofile(f)
    sub_folder = get_destination_subfolder(audiofile)
    if sub_folder is None:
      logging.warning('skipping %s' % f)
      continue

    base = os.path.basename(f)
    dest_path = os.path.join(destination, sub_folder)

    if not os.path.isdir(dest_path):
      if os.path.isfile(dest_path):
        logging.warning('destination path exists and is a file: %s', dest_path)
        continue
      os.makedirs(dest_path)

    dest_fname = os.path.join(dest_path, base)
    ext = base.split('.')[-1].lower()
    if ext != 'mp3' and transcode:
      dest_fname = dest_fname.replace('.%s' % ext, '.mp3')

    if os.path.isfile(dest_fname) and not overwrite:
      logging.info('%s exists, skipping' % dest_fname)
      continue

    # Obviously we should do this work in a threadpool
    if ext != 'mp3' and transcode:
      t = threading.Thread(target=transcode_file, args=(pool_sema, f, dest_fname, dryrun))
      t.start()
      threads.append(t)
    else:
      try:
        os.stat(dest_fname)
        if not overwrite:
          logging.warning('file %s exists, skipping' % dest_fname)
          continue
      except:
        pass
      logging.info('%s -> %s' % (base, dest_fname))
      if not dryrun:
        shutil.copyfile(f, dest_fname)

    #if not dryrun:
    #dest_audiofile = ID3(dest_fname)
    #set_rating(dest_audiofile, rating_from_level(get_level(audiofile)))
  for t in threads:
    t.join()


if __name__ == '__main__':

  parser = argparse.ArgumentParser(description='move shit around')
  parser.add_argument('--library', type=str, help='directory where all your music is')
  parser.add_argument('--destination', type=str, help='directory where the music should go.')
  parser.add_argument('--transcode', default=False, action='store_true', help='if set, transcode files to mp3')
  parser.add_argument('--overwrite', default=False, action='store_true', help='if set, overwrite existing files')
  parser.add_argument('--dryrun', default=False, action='store_true', help="if set, don't actually do anything")

  args = parser.parse_args()

  if not args.library:
    logging.error('need to specify a library dir')
    parser.print_usage()
    sys.exit(1)
  elif not os.path.isdir(args.library):
    logging.error('%s not a directory' % args.library)
    sys.exit(1)

  if not args.destination:
    logging.error('need to specify a destination dir')
    parser.print_usage()
    sys.exit(1)
  elif not os.path.isdir(args.destination):
    logging.error('%s not a directory' % args.destination)
    sys.exit(1)

  if args.library == args.destination:
    logging.error('not supported yet??')
    sys.exit(1)

  main(args.library, args.destination, args.transcode, args.overwrite, args.dryrun)
