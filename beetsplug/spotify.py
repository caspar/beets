# This file is part of beets.
# Copyright 2019, Rahul Ahuja.
# Copyright 2022, Alok Saboo.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Adds Spotify release and track search support to the autotagger, along with
Spotify playlist construction.
"""

from __future__ import annotations

import base64
import collections
import json
import re
import time
import webbrowser
from typing import TYPE_CHECKING, Any, Literal, Sequence, Union

import confuse
import requests
import unidecode

from beets import ui
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.dbcore import types
from beets.library import Library
from beets.metadata_plugins import (
    IDResponse,
    SearchApiMetadataSourcePlugin,
    SearchFilter,
)

if TYPE_CHECKING:
    from beets.library import Library
    from beetsplug._typing import JSONDict

DEFAULT_WAITING_TIME = 5


class SearchResponseAlbums(IDResponse):
    """A response returned by the Spotify API.

    We only use items and disregard the pagination information.
    i.e. res["albums"]["items"][0].

    There are more fields in the response, but we only type
    the ones we currently use.

    see https://developer.spotify.com/documentation/web-api/reference/search
    """

    album_type: str
    available_markets: Sequence[str]
    name: str


class SearchResponseTracks(IDResponse):
    """A track response returned by the Spotify API."""

    album: SearchResponseAlbums
    available_markets: Sequence[str]
    popularity: int
    name: str


class APIError(Exception):
    pass


class SpotifyPlugin(
    SearchApiMetadataSourcePlugin[
        Union[SearchResponseAlbums, SearchResponseTracks]
    ]
):
    item_types = {
        "spotify_track_popularity": types.INTEGER,
        "spotify_acousticness": types.FLOAT,
        "spotify_danceability": types.FLOAT,
        "spotify_energy": types.FLOAT,
        "spotify_instrumentalness": types.FLOAT,
        "spotify_key": types.FLOAT,
        "spotify_liveness": types.FLOAT,
        "spotify_loudness": types.FLOAT,
        "spotify_mode": types.INTEGER,
        "spotify_speechiness": types.FLOAT,
        "spotify_tempo": types.FLOAT,
        "spotify_time_signature": types.INTEGER,
        "spotify_valence": types.FLOAT,
        "spotify_updated": types.DATE,
    }

    # Base URLs for the Spotify API
    # Documentation: https://developer.spotify.com/web-api
    oauth_token_url = "https://accounts.spotify.com/api/token"
    open_track_url = "https://open.spotify.com/track/"
    search_url = "https://api.spotify.com/v1/search"
    album_url = "https://api.spotify.com/v1/albums/"
    track_url = "https://api.spotify.com/v1/tracks/"
    audio_features_url = "https://api.spotify.com/v1/audio-features/"

    spotify_audio_features = {
        "acousticness": "spotify_acousticness",
        "danceability": "spotify_danceability",
        "energy": "spotify_energy",
        "instrumentalness": "spotify_instrumentalness",
        "key": "spotify_key",
        "liveness": "spotify_liveness",
        "loudness": "spotify_loudness",
        "mode": "spotify_mode",
        "speechiness": "spotify_speechiness",
        "tempo": "spotify_tempo",
        "time_signature": "spotify_time_signature",
        "valence": "spotify_valence",
    }

    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "mode": "list",
                "tiebreak": "popularity",
                "show_failures": False,
                "artist_field": "albumartist",
                "album_field": "album",
                "track_field": "title",
                "region_filter": None,
                "regex": [],
                "client_id": "4e414367a1d14c75a5c5129a627fcab8",
                "client_secret": "f82bdc09b2254f1a8286815d02fd46dc",
                "tokenfile": "spotify_token.json",
                "search_query_ascii": False,
            }
        )
        self.config["client_id"].redact = True
        self.config["client_secret"].redact = True

        self.setup()

    def setup(self):
        """Retrieve previously saved OAuth token or generate a new one."""

        try:
            with open(self._tokenfile()) as f:
                token_data = json.load(f)
        except OSError:
            self._authenticate()
        else:
            self.access_token = token_data["access_token"]

    def _tokenfile(self) -> str:
        """Get the path to the JSON file for storing the OAuth token."""
        return self.config["tokenfile"].get(confuse.Filename(in_app_dir=True))

    def _authenticate(self) -> None:
        """Request an access token via the Client Credentials Flow:
        https://developer.spotify.com/documentation/general/guides/authorization-guide/#client-credentials-flow
        """
        c_id: str = self.config["client_id"].as_str()
        c_secret: str = self.config["client_secret"].as_str()

        headers = {
            "Authorization": "Basic {}".format(
                base64.b64encode(f"{c_id}:{c_secret}".encode()).decode()
            )
        }
        response = requests.post(
            self.oauth_token_url,
            data={"grant_type": "client_credentials"},
            headers=headers,
            timeout=10,
        )
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise ui.UserError(
                "Spotify authorization failed: {}\n{}".format(e, response.text)
            )
        self.access_token = response.json()["access_token"]

        # Save the token for later use.
        self._log.debug(
            "{} access token: {}", self.data_source, self.access_token
        )
        with open(self._tokenfile(), "w") as f:
            json.dump({"access_token": self.access_token}, f)

    def _handle_response(
        self,
        method: Literal["get", "post", "put", "delete"],
        url: str,
        params: Any = None,
        retry_count: int = 0,
        max_retries: int = 3,
    ) -> JSONDict:
        """Send a request, reauthenticating if necessary.

        :param method: HTTP method to use for the request.
        :param url: URL for the new :class:`Request` object.
        :param params: (optional) list of tuples or bytes to send
            in the query string for the :class:`Request`.
        :type params: dict
        """

        if retry_count > max_retries:
            raise APIError("Maximum retries reached.")

        try:
            response = requests.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ReadTimeout:
            self._log.error("ReadTimeout.")
            raise APIError("Request timed out.")
        except requests.exceptions.ConnectionError as e:
            self._log.error(f"Network error: {e}")
            raise APIError("Network error.")
        except requests.exceptions.RequestException as e:
            if e.response is None:
                self._log.error(f"Request failed: {e}")
                raise APIError("Request failed.")
            if e.response.status_code == 401:
                self._log.debug(
                    f"{self.data_source} access token has expired. "
                    f"Reauthenticating."
                )
                self._authenticate()
                return self._handle_response(
                    method,
                    url,
                    params=params,
                    retry_count=retry_count + 1,
                )
            elif e.response.status_code == 404:
                raise APIError(
                    f"API Error: {e.response.status_code}\n"
                    f"URL: {url}\nparams: {params}"
                )
            elif e.response.status_code == 429:
                seconds = e.response.headers.get(
                    "Retry-After", DEFAULT_WAITING_TIME
                )
                self._log.debug(
                    f"Too many API requests. Retrying after {seconds} seconds."
                )
                time.sleep(int(seconds) + 1)
                return self._handle_response(
                    method,
                    url,
                    params=params,
                    retry_count=retry_count + 1,
                )
            elif e.response.status_code == 503:
                self._log.error("Service Unavailable.")
                raise APIError("Service Unavailable.")
            elif e.response.status_code == 502:
                self._log.error("Bad Gateway.")
                raise APIError("Bad Gateway.")
            elif e.response is not None:
                raise APIError(
                    f"{self.data_source} API error:\n{e.response.text}\n"
                    f"URL:\n{url}\nparams:\n{params}"
                )
            else:
                self._log.error(f"Request failed. Error: {e}")
                raise APIError("Request failed.")

    def album_for_id(self, album_id: str) -> AlbumInfo | None:
        """Fetch an album by its Spotify ID or URL and return an
        AlbumInfo object or None if the album is not found.

        :param album_id: Spotify ID or URL for the album
        :type album_id: str
        :return: AlbumInfo object for album
        :rtype: beets.autotag.hooks.AlbumInfo or None
        """
        if not (spotify_id := self._extract_id(album_id)):
            return None

        album_data = self._handle_response("get", self.album_url + spotify_id)
        if album_data["name"] == "":
            self._log.debug("Album removed from Spotify: {}", album_id)
            return None
        artist, artist_id = self.get_artist(album_data["artists"])

        date_parts = [
            int(part) for part in album_data["release_date"].split("-")
        ]

        release_date_precision = album_data["release_date_precision"]
        if release_date_precision == "day":
            year, month, day = date_parts
        elif release_date_precision == "month":
            year, month = date_parts
            day = None
        elif release_date_precision == "year":
            year = date_parts[0]
            month = None
            day = None
        else:
            raise ui.UserError(
                "Invalid `release_date_precision` returned "
                "by {} API: '{}'".format(
                    self.data_source, release_date_precision
                )
            )

        tracks_data = album_data["tracks"]
        tracks_items = tracks_data["items"]
        while tracks_data["next"]:
            tracks_data = self._handle_response("get", tracks_data["next"])
            tracks_items.extend(tracks_data["items"])

        tracks = []
        medium_totals: dict[int | None, int] = collections.defaultdict(int)
        for i, track_data in enumerate(tracks_items, start=1):
            track = self._get_track(track_data)
            track.index = i
            medium_totals[track.medium] += 1
            tracks.append(track)
        for track in tracks:
            track.medium_total = medium_totals[track.medium]

        return AlbumInfo(
            album=album_data["name"],
            album_id=spotify_id,
            spotify_album_id=spotify_id,
            artist=artist,
            artist_id=artist_id,
            spotify_artist_id=artist_id,
            tracks=tracks,
            albumtype=album_data["album_type"],
            va=len(album_data["artists"]) == 1
            and artist.lower() == "various artists",
            year=year,
            month=month,
            day=day,
            label=album_data["label"],
            mediums=max(filter(None, medium_totals.keys())),
            data_source=self.data_source,
            data_url=album_data["external_urls"]["spotify"],
        )

    def _get_track(self, track_data: JSONDict) -> TrackInfo:
        """Convert a Spotify track object dict to a TrackInfo object.

        :param track_data: Simplified track object
            (https://developer.spotify.com/documentation/web-api/reference/object-model/#track-object-simplified)
        :return: TrackInfo object for track
        """
        artist, artist_id = self.get_artist(track_data["artists"])

        # Get album information for spotify tracks
        try:
            album = track_data["album"]["name"]
        except (KeyError, TypeError):
            album = None
        return TrackInfo(
            title=track_data["name"],
            track_id=track_data["id"],
            spotify_track_id=track_data["id"],
            artist=artist,
            album=album,
            artist_id=artist_id,
            spotify_artist_id=artist_id,
            length=track_data["duration_ms"] / 1000,
            index=track_data["track_number"],
            medium=track_data["disc_number"],
            medium_index=track_data["track_number"],
            data_source=self.data_source,
            data_url=track_data["external_urls"]["spotify"],
        )

    def track_for_id(self, track_id: str) -> None | TrackInfo:
        """Fetch a track by its Spotify ID or URL.

        Returns a TrackInfo object or None if the track is not found.
        """

        if not (spotify_id := self._extract_id(track_id)):
            self._log.debug("Invalid Spotify ID: {}", track_id)
            return None

        if not (
            track_data := self._handle_response(
                "get", f"{self.track_url}{spotify_id}"
            )
        ):
            self._log.debug("Track not found: {}", track_id)
            return None

        track = self._get_track(track_data)

        # Get album's tracks to set `track.index` (position on the entire
        # release) and `track.medium_total` (total number of tracks on
        # the track's disc).
        album_data = self._handle_response(
            "get", self.album_url + track_data["album"]["id"]
        )
        medium_total = 0
        for i, track_data in enumerate(album_data["tracks"]["items"], start=1):
            if track_data["disc_number"] == track.medium:
                medium_total += 1
                if track_data["id"] == track.track_id:
                    track.index = i
        track.medium_total = medium_total
        return track

    def _construct_search_query(
        self, filters: SearchFilter, keywords: str = ""
    ) -> str:
        """Construct a query string with the specified filters and keywords to
        be provided to the Spotify Search API
        (https://developer.spotify.com/documentation/web-api/reference/search).

        :param filters: (Optional) Field filters to apply.
        :param keywords: (Optional) Query keywords to use.
        :return: Query string to be provided to the Search API.
        """

        query_components = [
            keywords,
            " ".join(f"{k}:{v}" for k, v in filters.items()),
        ]
        query = " ".join([q for q in query_components if q])
        if not isinstance(query, str):
            query = query.decode("utf8")

        if self.config["search_query_ascii"].get():
            query = unidecode.unidecode(query)

        return query

    def _search_api(
        self,
        query_type: Literal["album", "track"],
        filters: SearchFilter,
        keywords: str = "",
    ) -> Sequence[SearchResponseAlbums | SearchResponseTracks]:
        """Query the Spotify Search API for the specified ``keywords``,
        applying the provided ``filters``.

        :param query_type: Item type to search across. Valid types are:
            'album', 'artist', 'playlist', and 'track'.
        :param filters: (Optional) Field filters to apply.
        :param keywords: (Optional) Query keywords to use.
        """
        query = self._construct_search_query(keywords=keywords, filters=filters)

        self._log.debug(f"Searching {self.data_source} for '{query}'")
        try:
            response = self._handle_response(
                "get",
                self.search_url,
                params={"q": query, "type": query_type},
            )
        except APIError as e:
            self._log.debug("Spotify API error: {}", e)
            return ()
        response_data = response.get(query_type + "s", {}).get("items", [])
        self._log.debug(
            "Found {} result(s) from {} for '{}'",
            len(response_data),
            self.data_source,
            query,
        )
        return response_data

    def commands(self) -> list[ui.Subcommand]:
        # autotagger import command
        def queries(lib, opts, args):
            success = self._parse_opts(opts)
            if success:
                results = self._match_library_tracks(lib, args)
                self._output_match_results(results)

        spotify_cmd = ui.Subcommand(
            "spotify", help=f"build a {self.data_source} playlist"
        )
        spotify_cmd.parser.add_option(
            "-m",
            "--mode",
            action="store",
            help='"open" to open {} with playlist, '
            '"list" to print (default)'.format(self.data_source),
        )
        spotify_cmd.parser.add_option(
            "-f",
            "--show-failures",
            action="store_true",
            dest="show_failures",
            help="list tracks that did not match a {} ID".format(
                self.data_source
            ),
        )
        spotify_cmd.func = queries

        # spotifysync command
        sync_cmd = ui.Subcommand(
            "spotifysync", help="fetch track attributes from Spotify"
        )
        sync_cmd.parser.add_option(
            "-f",
            "--force",
            dest="force_refetch",
            action="store_true",
            default=False,
            help="re-download data when already present",
        )

        def func(lib, opts, args):
            items = lib.items(args)
            self._fetch_info(items, ui.should_write(), opts.force_refetch)

        sync_cmd.func = func
        return [spotify_cmd, sync_cmd]

    def _parse_opts(self, opts):
        if opts.mode:
            self.config["mode"].set(opts.mode)

        if opts.show_failures:
            self.config["show_failures"].set(True)

        if self.config["mode"].get() not in ["list", "open"]:
            self._log.warning(
                "{0} is not a valid mode", self.config["mode"].get()
            )
            return False

        self.opts = opts
        return True

    def _match_library_tracks(self, library: Library, keywords: str):
        """Get a list of simplified track object dicts for library tracks
        matching the specified ``keywords``.

        :param library: beets library object to query.
        :param keywords: Query to match library items against.
        :return: List of simplified track object dicts for library items
            matching the specified query.
        """
        results = []
        failures = []

        items = library.items(keywords)

        if not items:
            self._log.debug(
                "Your beets query returned no items, skipping {}.",
                self.data_source,
            )
            return

        self._log.info("Processing {} tracks...", len(items))

        for item in items:
            # Apply regex transformations if provided
            for regex in self.config["regex"].get():
                if (
                    not regex["field"]
                    or not regex["search"]
                    or not regex["replace"]
                ):
                    continue

                value = item[regex["field"]]
                item[regex["field"]] = re.sub(
                    regex["search"], regex["replace"], value
                )

            # Custom values can be passed in the config (just in case)
            artist = item[self.config["artist_field"].get()]
            album = item[self.config["album_field"].get()]
            keywords = item[self.config["track_field"].get()]

            # Query the Web API for each track, look for the items' JSON data
            query_filters: SearchFilter = {"artist": artist, "album": album}
            response_data_tracks = self._search_api(
                query_type="track", keywords=keywords, filters=query_filters
            )
            if not response_data_tracks:
                query = self._construct_search_query(
                    keywords=keywords, filters=query_filters
                )

                failures.append(query)
                continue

            # Apply market filter if requested
            region_filter: str = self.config["region_filter"].get()
            if region_filter:
                response_data_tracks = [
                    track_data
                    for track_data in response_data_tracks
                    if region_filter in track_data["available_markets"]
                ]

            if (
                len(response_data_tracks) == 1
                or self.config["tiebreak"].get() == "first"
            ):
                self._log.debug(
                    "{} track(s) found, count: {}",
                    self.data_source,
                    len(response_data_tracks),
                )
                chosen_result = response_data_tracks[0]
            else:
                # Use the popularity filter
                self._log.debug(
                    "Most popular track chosen, count: {}",
                    len(response_data_tracks),
                )
                chosen_result = max(
                    response_data_tracks,
                    key=lambda x: x[
                        # We are sure this is a track response!
                        "popularity"  # type: ignore[typeddict-item]
                    ],
                )
            results.append(chosen_result)

        failure_count = len(failures)
        if failure_count > 0:
            if self.config["show_failures"].get():
                self._log.info(
                    "{} track(s) did not match a {} ID:",
                    failure_count,
                    self.data_source,
                )
                for track in failures:
                    self._log.info("track: {}", track)
                self._log.info("")
            else:
                self._log.warning(
                    "{} track(s) did not match a {} ID:\n"
                    "use --show-failures to display",
                    failure_count,
                    self.data_source,
                )

        return results

    def _output_match_results(self, results):
        """Open a playlist or print Spotify URLs for the provided track
        object dicts.

        :param results: List of simplified track object dicts
            (https://developer.spotify.com/documentation/web-api/reference/object-model/#track-object-simplified)
        :type results: list[dict]
        """
        if results:
            spotify_ids = [track_data["id"] for track_data in results]
            if self.config["mode"].get() == "open":
                self._log.info(
                    "Attempting to open {} with playlist".format(
                        self.data_source
                    )
                )
                spotify_url = "spotify:trackset:Playlist:" + ",".join(
                    spotify_ids
                )
                webbrowser.open(spotify_url)
            else:
                for spotify_id in spotify_ids:
                    print(self.open_track_url + spotify_id)
        else:
            self._log.warning(
                f"No {self.data_source} tracks found from beets query"
            )

    def _fetch_info(self, items, write, force):
        """Obtain track information from Spotify."""

        self._log.debug("Total {} tracks", len(items))

        for index, item in enumerate(items, start=1):
            self._log.info(
                "Processing {}/{} tracks - {} ", index, len(items), item
            )
            # If we're not forcing re-downloading for all tracks, check
            # whether the popularity data is already present
            if not force:
                if "spotify_track_popularity" in item:
                    self._log.debug("Popularity already present for: {}", item)
                    continue
            try:
                spotify_track_id = item.spotify_track_id
            except AttributeError:
                self._log.debug("No track_id present for: {}", item)
                continue

            popularity, isrc, ean, upc = self.track_info(spotify_track_id)
            item["spotify_track_popularity"] = popularity
            item["isrc"] = isrc
            item["ean"] = ean
            item["upc"] = upc
            audio_features = self.track_audio_features(spotify_track_id)
            if audio_features is None:
                self._log.info("No audio features found for: {}", item)
                continue
            for feature in audio_features.keys():
                if feature in self.spotify_audio_features.keys():
                    item[self.spotify_audio_features[feature]] = audio_features[
                        feature
                    ]
            item["spotify_updated"] = time.time()
            item.store()
            if write:
                item.try_write()

    def track_info(self, track_id: str):
        """Fetch a track's popularity and external IDs using its Spotify ID."""
        track_data = self._handle_response("get", self.track_url + track_id)
        external_ids = track_data.get("external_ids", {})
        popularity = track_data.get("popularity")
        self._log.debug(
            "track_popularity: {} and track_isrc: {}",
            popularity,
            external_ids.get("isrc"),
        )
        return (
            popularity,
            external_ids.get("isrc"),
            external_ids.get("ean"),
            external_ids.get("upc"),
        )

    def track_audio_features(self, track_id: str):
        """Fetch track audio features by its Spotify ID."""
        try:
            return self._handle_response(
                "get", self.audio_features_url + track_id
            )
        except APIError as e:
            self._log.debug("Spotify API error: {}", e)
            return None
