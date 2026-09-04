# -*- coding: utf-8 -*-
"""Kodi video plugin for video.aktualne.cz."""
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
import routing
import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

_addon = xbmcaddon.Addon()
_addon_id = _addon.getAddonInfo('id')
_addon_name = _addon.getAddonInfo('name')

plugin = routing.Plugin()

_RSS_BASE = 'https://www.aktualne.cz/rss'
# Slug of the feed that aggregates every video on the site.
_ROOT_SLUG = 'video'
# Only sections living on this host are real shows; the other categories seen in
# the video feed (zpravy/, magazin/, ...) have feeds full of text-only articles.
_SHOW_HOST = 'video.aktualne.cz'

_ATOM_LINK = '{http://www.w3.org/2005/Atom}link'
_PLAYER_ATTR = 'data-e-cra-media-cloud-player-remote-manifest-{0}-url-value'
_MIME_HLS = 'application/vnd.apple.mpegurl'
_MIME_DASH = 'application/dash+xml'

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                  ' (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
}
# (connect, read) - without this a stalled socket freezes the Kodi UI for good.
_TIMEOUT = (5, 15)

# How a video is labelled in the mixed "recent videos" list: the show name
# leads, the video title follows. Colour names in [COLOR] are resolved by the
# skin, so an unknown one renders invisible - use an explicit AARRGGBB value.
_SHOW_LABEL = '[COLOR FF7ACAFE]{0}[/COLOR] · {1}'

_SHOW_CACHE_TTL = 12 * 60 * 60
_DISCOVERY_PAGES = 16

_session = None


def _L(string_id):
    return _addon.getLocalizedString(string_id)


# ---------------------------------------------------------------- http / feeds

def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(_HEADERS)
    return _session


def _http_get(url):
    response = _get_session().get(url, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.content


def _text(element, default=''):
    if element is None or element.text is None:
        return default
    return element.text.strip()


def _feed_url(slug, page=1):
    url = '{0}/{1}/'.format(_RSS_BASE, slug.strip('/'))
    if page > 1:
        url = '{0}?page={1}'.format(url, page)
    return url


def _fetch_channel(url):
    channel = ET.fromstring(_http_get(url)).find('channel')
    if channel is None:
        raise ValueError('no <channel> element in {0}'.format(url))
    return channel


def _next_page(channel, current_page):
    """Page number the feed itself advertises, or None on the last page."""
    for link in channel.findall(_ATOM_LINK):
        if link.get('rel') != 'next':
            continue
        match = re.search(r'[?&]page=(\d+)', link.get('href') or '')
        return int(match.group(1)) if match else current_page + 1
    return None


def _channel_show_title(channel):
    # 'Spotlight | Aktualne.cz' -> 'Spotlight'
    title = _text(channel.find('title'))
    return title.split('|')[0].strip() or title


def _show_slug(domain):
    """Show slug from a <category domain="..."> URL, '' if it is not a show."""
    if not domain:
        return ''
    parsed = urlparse(domain)
    if parsed.netloc != _SHOW_HOST:
        return ''
    segments = [segment for segment in parsed.path.split('/') if segment]
    # Sub-sections (spotlight-video/spotlight-news) have no feed of their own,
    # so they fold into their parent show.
    return segments[0] if segments else ''


def _item_image(item):
    for enclosure in item.findall('enclosure'):
        if (enclosure.get('type') or '').startswith('image/'):
            return enclosure.get('url') or ''
    return ''


def _parse_date(value):
    if not value:
        return None
    try:
        return dateparser.parse(value)
    except (ValueError, OverflowError, TypeError):
        return None


# -------------------------------------------------------------------- listitem

def _set_video_info(list_item, title='', show_title='', plot='', premiered='',
                    duration=0, media_type='episode'):
    """setInfo() is deprecated since Kodi 20, its replacement absent in 19."""
    tag = list_item.getVideoInfoTag()
    if hasattr(tag, 'setMediaType'):
        tag.setMediaType(media_type)
        if title:
            tag.setTitle(title)
        if show_title:
            tag.setTvShowTitle(show_title)
        if plot:
            tag.setPlot(plot)
        if premiered:
            tag.setPremiered(premiered)
        if duration:
            tag.setDuration(duration)
        return

    info = {'mediatype': media_type}
    if title:
        info['title'] = title
    if show_title:
        info['tvshowtitle'] = show_title
    if plot:
        info['plot'] = plot
    if premiered:
        info['premiered'] = premiered
    if duration:
        info['duration'] = duration
    list_item.setInfo('video', info)


def _add_sort_methods():
    for method in (xbmcplugin.SORT_METHOD_UNSORTED,
                   xbmcplugin.SORT_METHOD_DATE,
                   xbmcplugin.SORT_METHOD_LABEL):
        xbmcplugin.addSortMethod(plugin.handle, method)


def _build_list_item(item, feed_title, show_badge):
    link = _text(item.find('link'))
    title = _text(item.find('title'))
    if not link or not title:
        return None

    category = item.find('category')
    show_title = _text(category) or feed_title
    label = title
    menu_items = []
    if show_badge:
        if show_title:
            label = _SHOW_LABEL.format(show_title, title)
        slug = _show_slug(category.get('domain') if category is not None else '')
        if slug:
            menu_items.append((_L(30004), 'Container.Update({0})'.format(
                plugin.url_for(get_list, show=slug, page=1))))

    published = _parse_date(_text(item.find('pubDate')))
    list_item = xbmcgui.ListItem(label)
    # With a content type set, Kodi rebuilds the displayed label from the info
    # tag title, so the show prefix has to live there too or it never shows up.
    _set_video_info(list_item, title=label, show_title=show_title,
                    plot=_text(item.find('description')),
                    premiered=published.strftime('%Y-%m-%d') if published else '')
    if published:
        list_item.setDateTime(published.strftime('%Y-%m-%dT%H:%M:%S'))
    image = _item_image(item)
    if image:
        list_item.setArt({'thumb': image, 'icon': image, 'fanart': image})
    list_item.setProperty('IsPlayable', 'true')
    if menu_items:
        list_item.addContextMenuItems(menu_items)
    return (plugin.url_for(get_video, link), list_item, False)


# ------------------------------------------------------------------ discovery

def _cache_path():
    profile = xbmcvfs.translatePath(_addon.getAddonInfo('profile'))
    return os.path.join(profile, 'shows.json')


def _read_show_cache():
    path = _cache_path()
    try:
        if not os.path.exists(path):
            return None
        if time.time() - os.path.getmtime(path) > _SHOW_CACHE_TTL:
            return None
        with open(path, 'r', encoding='utf-8') as handle:
            cached = json.load(handle)
    except (OSError, ValueError) as exc:
        _log_error(exc, 'reading show cache')
        return None
    if not isinstance(cached, list):
        return None
    shows = [(entry[0], entry[1]) for entry in cached
             if isinstance(entry, list) and len(entry) == 2]
    return shows or None


def _write_show_cache(shows):
    path = _cache_path()
    try:
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump([list(show) for show in shows], handle)
    except OSError as exc:
        _log_error(exc, 'writing show cache')


def _discover_shows():
    """Collect show slugs from the categories used in the main video feed."""
    titles = {}
    top_level = set()
    page = 1
    while page <= _DISCOVERY_PAGES:
        channel = _fetch_channel(_feed_url(_ROOT_SLUG, page))
        for item in channel.findall('item'):
            category = item.find('category')
            if category is None:
                continue
            domain = category.get('domain') or ''
            slug = _show_slug(domain)
            title = _text(category)
            if not slug or not title:
                continue
            is_top = len([s for s in urlparse(domain).path.split('/') if s]) == 1
            # A sub-section only names the show until the show itself shows up.
            if slug not in titles or (is_top and slug not in top_level):
                titles[slug] = title
            if is_top:
                top_level.add(slug)
        next_page = _next_page(channel, page)
        if next_page is None:
            break
        page = next_page
    return sorted(titles.items(), key=lambda show: show[1].lower())


def _shows():
    """Every show is read from the feed; nothing about them is hardcoded."""
    cached = _read_show_cache()
    if cached is not None:
        return cached
    shows = _discover_shows()
    if shows:
        _write_show_cache(shows)
    return shows


# ------------------------------------------------------------------- playback

def _player_attr(soup, protocol):
    attribute = _PLAYER_ATTR.format(protocol)
    node = soup.find(attrs={attribute: True})
    return (node[attribute] or '').strip() if node else ''


def _iter_ld_nodes(data):
    if isinstance(data, list):
        for entry in data:
            for node in _iter_ld_nodes(entry):
                yield node
    elif isinstance(data, dict):
        yield data
        for node in _iter_ld_nodes(data.get('@graph') or []):
            yield node


def _parse_iso_duration(value):
    if not isinstance(value, str):
        return 0
    match = re.match(r'^P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', value)
    if not match:
        return 0
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _video_metadata(soup):
    """Title, plot, duration and the MP4 fallback from the JSON-LD VideoObject."""
    for script in soup.find_all('script', {'type': 'application/ld+json'}):
        payload = script.get_text()
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        for node in _iter_ld_nodes(data):
            if node.get('@type') != 'VideoObject':
                continue
            thumbnail = node.get('thumbnailUrl') or ''
            if isinstance(thumbnail, list):
                thumbnail = thumbnail[0] if thumbnail else ''
            return {
                'title': node.get('name') or '',
                'plot': node.get('description') or '',
                'thumb': thumbnail,
                'content_url': node.get('contentUrl') or '',
                'duration': _parse_iso_duration(node.get('duration')),
            }
    return {}


def _resolve_stream(url):
    soup = BeautifulSoup(_http_get(url), 'html.parser')
    metadata = _video_metadata(soup)

    hls = _player_attr(soup, 'hls')
    if hls:
        return hls, _MIME_HLS, metadata
    if metadata.get('content_url'):
        return metadata['content_url'], '', metadata
    dash = _player_attr(soup, 'dash')
    if dash:
        return dash, _MIME_DASH, metadata
    return '', '', metadata


def _kodi_major():
    try:
        return int(xbmc.getInfoLabel('System.BuildVersion').split('.')[0])
    except (ValueError, IndexError):
        return 0


def _addon_enabled(addon_id):
    request = json.dumps({'jsonrpc': '2.0', 'id': 1,
                          'method': 'Addons.GetAddonDetails',
                          'params': {'addonid': addon_id,
                                     'properties': ['enabled']}})
    try:
        response = json.loads(xbmc.executeJSONRPC(request))
    except ValueError:
        return False
    return bool(response.get('result', {}).get('addon', {}).get('enabled'))


def _apply_inputstream(list_item, mime_type):
    if mime_type not in (_MIME_HLS, _MIME_DASH):
        return
    if not _addon_enabled('inputstream.adaptive'):
        return
    list_item.setProperty('inputstream', 'inputstream.adaptive')
    # Kodi 21+ infers the manifest type from the mime type and warns about this.
    if _kodi_major() < 21:
        list_item.setProperty('inputstream.adaptive.manifest_type',
                              'hls' if mime_type == _MIME_HLS else 'mpd')


# --------------------------------------------------------------------- errors

def _log_error(exc, context):
    xbmc.log('{0}: {1} failed: {2}: {3}'.format(
        _addon_id, context, type(exc).__name__, exc), xbmc.LOGERROR)


def _notify(message):
    xbmcgui.Dialog().notification(_addon_name, message,
                                  xbmcgui.NOTIFICATION_ERROR)


# --------------------------------------------------------------------- routes

def _arg(name, default=''):
    values = plugin.args.get(name)
    return values[0] if values else default


def _int_arg(name, default=0):
    try:
        return int(_arg(name, default))
    except (TypeError, ValueError):
        return default


@plugin.route('/')
def root():
    listing = []

    # Icons only - a thumb here would squash the addon logo into the list.
    list_item = xbmcgui.ListItem(_L(30001))
    list_item.setArt({'icon': 'DefaultRecentlyAddedEpisodes.png'})
    listing.append((plugin.url_for(get_list, show=_ROOT_SLUG, page=1,
                                   category=1), list_item, True))

    list_item = xbmcgui.ListItem(_L(30002))
    list_item.setArt({'icon': 'DefaultTVShows.png'})
    listing.append((plugin.url_for(list_shows), list_item, True))

    xbmcplugin.addDirectoryItems(plugin.handle, listing, len(listing))
    xbmcplugin.endOfDirectory(plugin.handle)


@plugin.route('/list_shows/')
def list_shows():
    xbmcplugin.setContent(plugin.handle, 'tvshows')
    xbmcplugin.setPluginCategory(plugin.handle, _L(30002))

    try:
        shows = _shows()
    except Exception as exc:
        _log_error(exc, 'show discovery')
        _notify(_L(30007))
        xbmcplugin.endOfDirectory(plugin.handle, succeeded=False)
        return

    listing = []
    for slug, title in shows:
        list_item = xbmcgui.ListItem(title)
        _set_video_info(list_item, title=title, media_type='tvshow')
        list_item.setArt({'icon': 'DefaultTVShows.png'})
        listing.append((plugin.url_for(get_list, show=slug, page=1),
                        list_item, True))

    xbmcplugin.addDirectoryItems(plugin.handle, listing, len(listing))
    xbmcplugin.addSortMethod(plugin.handle, xbmcplugin.SORT_METHOD_LABEL)
    xbmcplugin.endOfDirectory(plugin.handle)


@plugin.route('/get_list/')
def get_list():
    xbmcplugin.setContent(plugin.handle, 'episodes')
    slug = _arg('show', _ROOT_SLUG)
    page = max(1, _int_arg('page', 1))
    show_badge = _int_arg('category', 0) == 1

    try:
        channel = _fetch_channel(_feed_url(slug, page))
    except Exception as exc:
        _log_error(exc, 'feed {0} page {1}'.format(slug, page))
        _notify(_L(30007))
        xbmcplugin.endOfDirectory(plugin.handle, succeeded=False)
        return

    feed_title = _channel_show_title(channel)
    xbmcplugin.setPluginCategory(plugin.handle, feed_title)

    listing = []
    for item in channel.findall('item'):
        entry = _build_list_item(item, feed_title, show_badge)
        if entry is not None:
            listing.append(entry)

    next_page = _next_page(channel, page)
    if listing and next_page is not None:
        list_item = xbmcgui.ListItem(_L(30003))
        list_item.setArt({'icon': 'DefaultFolder.png'})
        listing.append((plugin.url_for(get_list, show=slug, page=next_page,
                                       category=1 if show_badge else 0),
                        list_item, True))

    xbmcplugin.addDirectoryItems(plugin.handle, listing, len(listing))
    _add_sort_methods()
    xbmcplugin.endOfDirectory(plugin.handle)


@plugin.route('/get_video/<path:show_url>')
def get_video(show_url):
    stream_url, mime_type, metadata = '', '', {}
    try:
        stream_url, mime_type, metadata = _resolve_stream(show_url)
    except Exception as exc:
        _log_error(exc, 'resolving {0}'.format(show_url))

    if not stream_url:
        _notify(_L(30005))
        xbmcplugin.setResolvedUrl(plugin.handle, False, xbmcgui.ListItem())
        return

    list_item = xbmcgui.ListItem(path=stream_url)
    if mime_type:
        list_item.setMimeType(mime_type)
        list_item.setContentLookup(False)
    _apply_inputstream(list_item, mime_type)
    _set_video_info(list_item, title=metadata.get('title', ''),
                    plot=metadata.get('plot', ''),
                    duration=metadata.get('duration', 0))
    if metadata.get('thumb'):
        list_item.setArt({'thumb': metadata['thumb'],
                          'fanart': metadata['thumb']})
    xbmcplugin.setResolvedUrl(plugin.handle, True, list_item)


def run():
    plugin.run()
