<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Music and photos

One mode: the music plays and the photos go by at the same time, both in random
order, on the television. Installer component `media` (on by default): it
installs the two players and creates the media folder, `/medias` unless another
was chosen (`--media-dir`).

```
/medias/
  pictures/   photos, at the top or in sub-folders, as deep as you like
  music/      music, likewise
  archive/    what was set aside; never played
```

Files at the top and files in sub-folders are **one single shuffled list**: keep
albums in folders or drop everything in loose, both work.

The music is played by `mpg123` and the photos by `fbi`, both chosen because
they are light enough for a Pi 1: the whole mode takes about 13% of its single
core. A photo stays up 15 seconds (`MAMAN_PHOTO_SECONDS` in
`/etc/maman-tv-lite/api.env`).

## Preparing the photos first

Do this on a computer before copying anything to the box — the board cannot do
any of it for itself:

```bash
scripts/prepare-photos.sh ~/Photos/maman
```

It reads JPEG, HEIC, HEIF, PNG and TIFF from that folder and writes a `resized`
folder beside them; copy `resized` into the share as `pictures`. A second
argument gives the **television's** size — `FULL-HD` by default, or `HD` or
`4K` — and a third the JPEG quality, 82 by default. It:

- **turns HEIC into JPEG**: an iPhone writes HEIC, which nothing on the board
  can decode;
- **bakes in the rotation**, so a photo taken sideways comes up the right way;
- **converts the colours to sRGB**, or an iPhone's photos come out
  over-saturated;
- **shrinks the photo to the screen**, so it comes up in about a second.

The music must be MPEG audio (MP3). A `.flac` or an `.m4a` is not played.

## The two buttons

| Now | "Music" button | "TV" button |
|---|---|---|
| TV off | TV on, on the box's input, music and photos | TV on, on its programmes |
| TV on its programmes | music and photos on the box's input | TV off |
| Music playing | stop, TV off | stop, back to the TV's programmes |

The phone page offers the same, plus one music folder at a time, previous and
next, and setting a photo or a track aside.

**The music and the photos stop when the television goes off**, however it was
switched off: while something plays, the box asks the set every 30 seconds how
it is (`MAMAN_TV_WATCH_SECONDS`) and stops once it has answered "off" twice in a
row.

How the box gives the television back its programmes when the music stops is a
setting of its own — see [television.md](television.md).

## The API

| Route | What |
|---|---|
| `POST /mode/music` | what the Music button does. `folder` plays one music folder only |
| `POST /mode/television` | leave the mode: stop everything and switch the set off, or send it back to its programmes with `switch_off=false` |
| `GET /media/status` | playing or not, how many photos and tracks |
| `GET /media/folders` | the music folders that can be played on their own |
| `POST /media/music/next`, `/media/music/previous` | change track |
| `POST /media/pictures/next`, `/media/pictures/previous` | change photo without waiting for its turn |
| `POST /media/music/archive`, `/media/pictures/archive` | set aside what is playing, and move on |

### Playing one folder

```
POST /mode/music?folder=Tri Yann
```

The music is then that folder only; the photos stay the whole library. The
folders are read from the disk each time, so an album dropped on the share is
on offer at once, in Swagger's list and on the phone page.

### Setting a photo or a track aside

`archive` **moves** the file to `<media>/archive/pictures` or
`<media>/archive/music` and moves on. Nothing is ever deleted: a mistake is one
move back through the file share.

**Not straight after "previous photo"**: the box would set aside the photo
before it. Stepping forward is unaffected.

## Adding files from a computer — the share

Installer component `share` (off by default; needs Tailscale). The media folder
appears as the network share `medias`:

- **Mac**: Finder → Go → Connect to Server → `smb://<Tailscale address>/medias`
- **Windows**: in the Explorer's address bar, `\\<Tailscale address>\medias`

User: the service account; password: the one chosen when the share was set up.
Running the installer again to update the box leaves it alone. To change it:
`sudo smbpasswd <account>`.

The share answers over Tailscale, and on the local network only if that was
asked for at installation (`--with-share-lan`): on a care home's WiFi it does
not exist for the other residents. Then it is also at
`smb://<hostname>.local/medias`, including over a cable plugged straight into
the box.

Without the share, `rsync` over Tailscale does the same:
`rsync -av photos/ <account>@<Tailscale address>:/medias/pictures/`.

## When it does not work

| Symptom | Look at |
|---|---|
| Photos but no sound | the TV's volume first; then `music:` lines in `journalctl -u maman-api`. `MAMAN_AUDIO_DEVICE` forces an output — `aplay -L` lists them |
| Sound but no photos | `journalctl -u maman-screen`, the service that draws them |
| The last photo stays on the television | `sudo systemctl restart maman-screen` |
| A photo shows nothing | a format the board cannot read — prepare the photos first |
| Nothing at all | `GET /media/status`: no photos and no music means empty folders, or files in the wrong one |
