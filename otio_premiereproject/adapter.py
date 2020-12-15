__doc__ = """OpenTimelineIO Adobe Premiere Project Adapter"""

from xml.etree import ElementTree as ET
import gzip
from copy import deepcopy
import base64

import opentimelineio as otio


class AdobePremiereProjectParseError(otio.exceptions.OTIOError):
    pass


def read_from_file(filepath):
    if hasattr(gzip, 'BadGzipFile'):
        exc_type = gzip.BadGzipFile
    else:
        exc_type = IOError
    try:
        input_str = gzip.GzipFile(filepath).read()
    except exc_type as e:
        if exc_type is IOError:
            if e.message != 'Not a gzipped file':
                raise
        input_str = open(filepath).read()
    return read_from_string(input_str)


def read_from_string(input_str):
    try:
        root = ET.fromstring(input_str)
    except ET.ParseError:
        raise AdobePremiereProjectParseError("Data is neither XML nor gzipped XML.")
    return AdobePremiereProject(root).to_collection()


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
        id_ = node.get("ObjectRef")
        if not id_:
            raise AdobePremiereProjectParseError(
                "Node has no ObjectRef attribute: {}".format(node)
            )
        return self._get_object(id_)

    def _udereference(self, node):
        uid = node.get("ObjectURef")
        if not uid:
            raise AdobePremiereProjectParseError(
                "Node has no ObjectURef attribute: {}".format(node)
            )
        return self._get_object(uid, "ObjectUID")

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
        frame_rate = self._frame_rate
        if frame_rate is None:
            frame_rate = next(
                (
                    254016000000
                    / int(n.find("TrackGroup/FrameRate").text)
                    for n in top_track_group_nodes
                    if n.tag == "VideoTrackGroup"
                ),
                25,
            )
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
                track = otio.schema.Track(kind=track_kind)
                last_track_end = 0
                for top_track_item_node in self._dereference_all(
                    track_node.findall("ClipTrack/ClipItems/TrackItems/TrackItem")
                ):
                    clip_track_item_node = top_track_item_node.find("ClipTrackItem")
                    track_item_node = clip_track_item_node.find("TrackItem")
                    track_start = int(track_item_node.find("Start").text) / 254016000000
                    track_end = int(track_item_node.find("End").text) / 254016000000
                    if track_start > last_track_end:
                        track.append(
                            otio.schema.Gap(
                                duration=otio.opentime.RationalTime(
                                    track_start - last_track_end
                                ).rescaled_to(frame_rate)
                            )
                        )
                    sub_clip_node = self._dereference(
                        clip_track_item_node.find("SubClip")
                    )
                    top_clip_node = self._dereference(sub_clip_node.find("Clip"))
                    clip_node = top_clip_node.find("Clip")
                    clip_in = otio.opentime.RationalTime(
                        int(clip_node.find("InPoint").text)
                        / 254016000000
                    ).rescaled_to(frame_rate)
                    clip_out = otio.opentime.RationalTime(
                        int(clip_node.find("OutPoint").text)
                        / 254016000000
                    ).rescaled_to(frame_rate)
                    media_source_node = self._dereference(clip_node.find("Source"))
                    media_node_ref = media_source_node.find("MediaSource/Media")
                    if media_node_ref is not None:
                        media_node = self._udereference(media_node_ref)
                        # could it be a generator rather than a file?
                        importer_prefs_node = media_node.find("ImporterPrefs")
                        if importer_prefs_node is not None:
                            media_reference = self._generator_reference_from_media_node(
                                media_node, importer_prefs_node
                            )
                        else:
                            media_reference = self._external_reference_from_media_node(
                                media_node, track_kind, frame_rate
                            )
                        clip = otio.schema.Clip(media_reference=media_reference)
                    else:
                        sequence_node_ref = media_source_node.find(
                            "SequenceSource/Sequence"
                        )
                        sequence_node = self._udereference(sequence_node_ref)
                        clip = self._stack_from_sequence_node(sequence_node)
                    clip.source_range = otio.opentime.TimeRange(
                        start_time=clip_in,
                        duration=clip_out - clip_in,
                    )
                    playback_speed_node = clip_node.find("PlaybackSpeed")
                    if playback_speed_node is not None:
                        speed = float(playback_speed_node.text)
                        clip.effects.append(
                            otio.schema.LinearTimeWarp(
                                time_scalar=speed,
                            )
                        )
                        # set clip source range accordingly
                        time_transform = otio.opentime.TimeTransform(
                            scale=1 / speed,
                        )
                        clip.source_range = otio.opentime.TimeRange(
                            start_time=clip.source_range.start_time,
                            duration=time_transform.applied_to(
                                clip.source_range.duration,
                            ),
                        )
                    track.append(clip)
                    last_track_end = track_end
                stack.append(track)
        return stack

    def _external_reference_from_media_node(self, media_node, track_kind, frame_rate):
        media_name = media_node.find("Title").text
        media_start = otio.opentime.RationalTime(
            int(media_node.find("Start").text)
            / 254016000000
        ).rescaled_to(frame_rate)
        if track_kind == "Video":
            video_stream_node = self._dereference(
                media_node.find("VideoStream")
            )
            media_duration = otio.opentime.RationalTime(
                int(video_stream_node.find("Duration").text)
                / 254016000000
            ).rescaled_to(frame_rate)
        elif track_kind == "Audio":
            video_stream_node = self._dereference(
                media_node.find("AudioStream")
            )
            media_duration = otio.opentime.RationalTime(
                int(video_stream_node.find("Duration").text)
                / 254016000000
            ).rescaled_to(frame_rate)
        else:
            raise NotImplementedError(
                "Can only handle Video or Audio here atm."
            )
        return otio.schema.ExternalReference(
            target_url=media_name,
            available_range=otio.opentime.TimeRange(
                start_time=media_start,
                duration=media_duration,
            ),
        )

    def _generator_reference_from_media_node(self, media_node, importer_prefs_node):
        return otio.schema.GeneratorReference(
            name=media_node.find("Title").text,
            generator_kind="premiere_generator",
            parameters=dict(
                importer_prefs=base64.b64decode(
                    importer_prefs_node.text
                ),
            ),
        )
