__doc__ = """OpenTimelineIO Adobe Premiere Project Adapter"""

from xml.etree import ElementTree as ET
import gzip
from copy import deepcopy
import base64

import opentimelineio as otio


PREMIERE_TICKS_PER_SECOND = 254016000000


def premiere_ticks_to_secs(t):
    return t / PREMIERE_TICKS_PER_SECOND


def premiere_ticks_to_fps(t):
    return PREMIERE_TICKS_PER_SECOND / t


def round_rational_time(rt):
    return otio.opentime.RationalTime(
        round(rt.value),
        rt.rate,
    )


class AdobePremiereProjectParseError(otio.exceptions.OTIOError):
    pass


def read_from_file(filepath, sequence_name=None):
    if hasattr(gzip, 'BadGzipFile'):
        exc_type = gzip.BadGzipFile
    else:
        exc_type = IOError
    was_gzipped = False
    try:
        input_str = gzip.GzipFile(filepath).read()
        was_gzipped = True
    except exc_type as e:
        if exc_type is IOError:
            if e.message != 'Not a gzipped file':
                raise
        input_str = open(filepath).read()
    try:
        return read_from_string(input_str, sequence_name=sequence_name)
    except AdobePremiereProjectParseError as e:
        if not was_gzipped:
            raise
        raise AdobePremiereProjectParseError("File is GZipped, but the data with is unrecognised: {}".format(e))


def read_from_string(input_str, sequence_name=None):
    try:
        root = ET.fromstring(input_str)
    except ET.ParseError as e:
        raise AdobePremiereProjectParseError("Data is not XML: {}".format(e))
    collection = AdobePremiereProject(root).to_collection()
    if sequence_name:
        timeline = next(
            (t for t in collection if t.name == sequence_name),
            None,
        )
        if timeline is None:
            raise KeyError("No sequence named: {}".format(sequence_name))
        return timeline
    return collection


class AdobePremiereProject(object):
    def __init__(self, root, frame_rate=None):
        self._root = root
        self._object_cache = {}
        self._frame_rate = frame_rate

    def to_collection(self):
        collection = otio.schema.SerializableCollection()

        for sequence_node in self._root.findall("Sequence"):
            stack = self._stack_from_sequence_node(sequence_node)
            timeline = otio.schema.Timeline(name=stack.name)
            timeline.tracks.extend(map(deepcopy, stack))
            collection.append(timeline)

        return collection

    def _get_object(self, id_, attr_name="ObjectID"):
        if id_ not in self._object_cache:
            node = self._root.find("*[@{}='{}']".format(attr_name, id_))
            if node is None or not len(node):
                raise AdobePremiereProjectParseError(
                    "Couldn't find object with {} {}".format(attr_name, id_)
                )
            self._object_cache[id_] = node
        return self._object_cache[id_]

    def _dereference(self, node):
        attr_name = "ObjectID"
        id_ = node.get("ObjectRef")
        if not id_:
            attr_name = "ObjectUID"
            id_ = node.get("ObjectURef")
            if not id_:
                raise AdobePremiereProjectParseError(
                    "Node has neither ObjectRef nor ObjectURef attribute: {}".format(node)
                )
        return self._get_object(id_, attr_name)

    def _dereference_all(self, nodes):
        return [
            self._dereference(node)
            for node in nodes
        ]

    def _stack_from_sequence_node(self, sequence_node):
        stack = otio.schema.Stack(name=sequence_node.find("Name").text)
        top_track_group_nodes = self._dereference_all(
            sequence_node.findall("TrackGroups/TrackGroup/Second")
        )
        premiere_sequence_frame_rate = next(
            (
                int(n.find("TrackGroup/FrameRate").text)
                for n in top_track_group_nodes
                if n.tag == "VideoTrackGroup"
            ),
            None,
        )
        if premiere_sequence_frame_rate is not None:
            stack.metadata['premiere'] = dict(
                frame_rate=premiere_sequence_frame_rate,
            )
        frame_rate = self._frame_rate
        if frame_rate is None:
            if premiere_sequence_frame_rate is not None:
                frame_rate = premiere_ticks_to_fps(
                    premiere_sequence_frame_rate
                )
            else:
                frame_rate = 25
        for top_track_group_node in top_track_group_nodes:
            for track_node in self._dereference_all(
                top_track_group_node.findall("TrackGroup/Tracks/Track")
            ):
                if track_node.tag == "VideoClipTrack":
                    track_kind = "Video"
                elif track_node.tag == "AudioClipTrack":
                    track_kind = "Audio"
                else:
                    raise AdobePremiereProjectParseError(
                        "Unknown track type: {} (ObjectID: {})".format(
                            track_node.tag, track_node.get("ObjectID")
                        )
                    )
                track = otio.schema.Track(
                    kind=track_kind,
                )
                last_track_end = 0
                for top_track_item_node in self._dereference_all(
                    track_node.findall("ClipTrack/ClipItems/TrackItems/TrackItem")
                ):
                    clip_track_item_node = top_track_item_node.find("ClipTrackItem")
                    track_item_node = clip_track_item_node.find("TrackItem")
                    premiere_track_start = int(track_item_node.find("Start").text)
                    premiere_track_end = int(track_item_node.find("End").text)
                    track_start = premiere_ticks_to_secs(premiere_track_start)
                    track_end = premiere_ticks_to_secs(premiere_track_end)
                    if track_start > last_track_end:
                        track.append(
                            otio.schema.Gap(
                                duration=round_rational_time(
                                    otio.opentime.RationalTime(
                                        track_start - last_track_end
                                    ).rescaled_to(frame_rate)
                                ),
                            )
                        )
                    sub_clip_node = self._dereference(
                        clip_track_item_node.find("SubClip")
                    )
                    top_clip_node = self._dereference(sub_clip_node.find("Clip"))
                    clip_node = top_clip_node.find("Clip")
                    premiere_clip_in = int(clip_node.find("InPoint").text)
                    premiere_clip_out = int(clip_node.find("OutPoint").text)
                    clip_in = round_rational_time(
                        otio.opentime.RationalTime(
                            premiere_ticks_to_secs(premiere_clip_in)
                        ).rescaled_to(frame_rate)
                    )
                    media_source_node = self._dereference(clip_node.find("Source"))
                    media_node_ref = media_source_node.find("MediaSource/Media")
                    if media_node_ref is not None:
                        media_node = self._dereference(media_node_ref)
                        # could it be a generator rather than a file?
                        file_key = media_node.find("FileKey").text
                        if file_key:
                            media_reference = self._external_reference_from_media_node(
                                media_node, track_kind, frame_rate
                            )
                        else:
                            media_reference = self._generator_reference_from_media_node(
                                media_node
                            )
                        clip = otio.schema.Clip(media_reference=media_reference)
                    else:
                        sequence_node_ref = media_source_node.find(
                            "SequenceSource/Sequence"
                        )
                        sequence_node = self._dereference(sequence_node_ref)
                        clip = self._stack_from_sequence_node(sequence_node)
                    clip.source_range = otio.opentime.TimeRange(
                        start_time=clip_in,
                        duration=round_rational_time(
                            otio.opentime.RationalTime(
                                track_end - track_start
                            ).rescaled_to(frame_rate)
                        ),
                    )
                    clip.metadata['premiere'] = dict(
                        track_start=premiere_track_start,
                        track_end=premiere_track_end,
                        clip_in=premiere_clip_in,
                        clip_out=premiere_clip_out,
                    )
                    playback_speed_node = clip_node.find("PlaybackSpeed")
                    if playback_speed_node is not None:
                        speed = float(playback_speed_node.text)
                        clip.effects.append(
                            otio.schema.LinearTimeWarp(
                                time_scalar=speed,
                            )
                        )
                    track.append(clip)
                    last_track_end = track_end
                stack.append(track)
        return stack

    def _external_reference_from_media_node(self, media_node, track_kind, frame_rate):
        media_name = media_node.find("Title").text
        start_node = media_node.find("Start")
        if start_node:
            premiere_media_start = int(start_node.text)
            media_start = otio.opentime.RationalTime(
                premiere_ticks_to_secs(premiere_media_start)
            ).rescaled_to(frame_rate)
        else:
            premiere_media_start = None
            media_start = otio.opentime.RationalTime(0, frame_rate)
        if track_kind == "Video":
            stream_node_name = "VideoStream"
        elif track_kind == "Audio":
            stream_node_name = "AudioStream"
        else:
            raise NotImplementedError(
                "Can only handle Video or Audio here atm."
            )
        stream_node = self._dereference(
            media_node.find(stream_node_name)
        )
        premiere_media_duration = int(stream_node.find("Duration").text)
        media_duration = otio.opentime.RationalTime(
            premiere_ticks_to_secs(premiere_media_duration)
        ).rescaled_to(frame_rate)
        return otio.schema.ExternalReference(
            target_url=media_name,
            available_range=otio.opentime.TimeRange(
                start_time=media_start,
                duration=media_duration,
            ),
            metadata=dict(
                premiere=dict(
                    media_start=premiere_media_start,
                    media_duration=premiere_media_duration,
                ),
            ),
        )

    def _generator_reference_from_media_node(self, media_node):
        gen_ref = otio.schema.GeneratorReference(
            name=media_node.find("Title").text,
            generator_kind="premiere_generator",
        )
        importer_prefs_node = media_node.find("ImporterPrefs")
        if importer_prefs_node is not None and importer_prefs_node.text is not None:
            gen_ref.parameters = dict(
                importer_prefs=base64.b64decode(
                    importer_prefs_node.text
                ),
            )
        return gen_ref
