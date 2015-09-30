#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
:copyright:
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2015
:license:
    GNU Lesser General Public License, Version 3 [non-commercial/academic use]
    (http://www.gnu.org/copyleft/lgpl.html)
"""
import io
import math
import numpy as np
import zipfile

import obspy
import tornado.gen
import tornado.web

from ... import FiniteSource
from ..util import run_async, IOQueue, _validtimesetting, \
    _validate_and_write_waveforms
from ..instaseis_request import InstaseisTimeSeriesHandler


@run_async
def _get_finite_source(db, finite_source, receiver, components, units, dt,
                       kernelwidth, starttime, endtime,
                       time_of_first_sample, format, label,
                       callback):
    """
    Extract a seismogram from the passed db and write it either to a MiniSEED
    or a SACZIP file.

    :param db: An open instaseis database.
    :param finite_source: An instaseis finite source.
    :param receiver: An instaseis receiver.
    :param components: The components.
    :param units: The desired units.
    :param remove_source_shift: Remove the source time shift or not.
    :param dt: dt to resample to.
    :param kernelwidth: Width of the interpolation kernel.
    :param starttime: The desired start time of the seismogram.
    :param endtime: The desired end time of the seismogram.
    :param time_of_first_sample: The time of the first sample.
    :param format: The output format. Either "miniseed" or "saczip".
    :param label: Prefix for the filename within the SAC zip file.
    :param callback: callback function of the coroutine.
    """
    try:
        st = db.get_seismograms_finite_source(
            sources=finite_source, receiver=receiver, components=components,
            kind=units, dt=dt, kernelwidth=kernelwidth)
    except Exception:
        msg = ("Could not extract seismogram. Make sure, the components "
               "are valid, and the depth settings are correct.")
        callback(tornado.web.HTTPError(400, log_message=msg, reason=msg))
        return

    for tr in st:
        tr.stats.starttime = time_of_first_sample

    finite_source.origin_time = time_of_first_sample + \
        finite_source.additional_time_shift

    _validate_and_write_waveforms(st=st, callback=callback,
                                  starttime=starttime, endtime=endtime,
                                  source=finite_source, receiver=receiver,
                                  db=db, label=label, format=format)


@run_async
def _parse_and_resample_finite_source(request, db_info, callback):
    try:
        with io.BytesIO(request.body) as buf:
            # We get 10.000 samples for each source sampled at 10 Hz. This is
            # more than enough to capture a minimal possible rise time of 1
            # second. The maximum possible time shift for any source is
            # therefore 1000 second which should be enough for any real fault.
            # Might need some more thought.
            finite_source = FiniteSource.from_usgs_param_file(
                buf, npts=10000, dt=0.1, trise_min=1.0)
    except:
        msg = ("Could not parse the body contents. Incorrect USGS param "
               "file?")
        callback(tornado.web.HTTPError(400, log_message=msg, reason=msg))
        return

    dominant_period = db_info.period

    # Here comes the magic. This is really messy but unfortunately very hard
    # to do.
    # Add two periods of samples at the beginning end the end to avoid
    # boundary effects at the ends.
    samples = int(math.ceil((2 * dominant_period / db_info.dt))) + 1
    zeros = np.zeros(samples)
    shift = samples * db_info.dt
    for source in finite_source.pointsources:
        source.sliprate = np.concatenate([zeros, source.sliprate, zeros])
        source.time_shift += shift

    finite_source.additional_time_shift = shift

    # A lowpass filter is needed to avoid aliasing - I guess using a
    # zerophase filter is a bit questionable as it has some potentially
    # acausal effects but it does not shift the times.
    finite_source.lp_sliprate(freq=1.0 / dominant_period, zerophase=True)

    # Last step is to resample to the sampling rate of the database for the
    # final convolution.
    finite_source.resample_sliprate(dt=db_info.dt,
                                    nsamp=db_info.npts + 2 * samples)

    # Will set the hypocentral coordinates.
    finite_source.find_hypocenter()

    callback(finite_source)


class FiniteSourceSeismogramsHandler(InstaseisTimeSeriesHandler):
    # Define the arguments for the seismogram endpoint.
    arguments = {
        "components": {"type": str, "default": "ZNE"},
        "units": {"type": str, "default": "displacement"},
        "dt": {"type": float},
        "kernelwidth": {"type": int, "default": 12},
        "label": {"type": str},

        # Time parameters.
        "origintime": {"type": obspy.UTCDateTime},
        "starttime": {"type": _validtimesetting,
                      "format": "Datetime String/Float/Phase+-Offset"},
        "endtime": {"type": _validtimesetting,
                    "format": "Datetime String/Float/Phase+-Offset"},

        # Receivers can be specified either directly via their coordinates.
        # In that case one can assign a network and station code.
        "receiverlatitude": {"type": float},
        "receiverlongitude": {"type": float},
        "networkcode": {"type": str, "default": "XX"},
        "stationcode": {"type": str, "default": "SYN"},

        # Or by querying a database.
        "network": {"type": str},
        "station": {"type": str},

        "format": {"type": str, "default": "saczip"}
    }

    default_label = "instaseis_finite_source_seismogram"
    # Done here as the time parsing is fairly complex and cannot be done
    # with normal default values.
    default_origin_time = obspy.UTCDateTime(1900, 1, 1)

    def __init__(self, *args, **kwargs):
        super(InstaseisTimeSeriesHandler, self).__init__(*args, **kwargs)

    def validate_parameters(self, args):
        """
        Function attempting to validate that the passed parameters are
        valid. Does not need to check the types as that has already been done.
        """
        self.validate_receiver_parameters(args)

    def parse_time_settings(self, args, finite_source):
        """
        Has to be overwritten as the finite source is a bit too different.
        """
        if args.origintime is None:
            args.origintime = self.default_origin_time

        # The origin time will be always set. If the starttime is not set,
        # set it to the origin time.
        if args.starttime is None:
            args.starttime = args.origintime

        # Now it becomes a bit ugly. If the starttime is a float, treat it
        # relative to the origin time.
        if isinstance(args.starttime, float):
            args.starttime = args.origintime + args.starttime

        # Now deal with the endtime.
        if isinstance(args.endtime, float):
            # If the start time is already known as an absolute time,
            # just add it.
            if isinstance(args.starttime, obspy.UTCDateTime):
                args.endtime = args.starttime + args.endtime
            # Otherwise the start time has to be a phase relative time and
            # is dealt with later.
            else:
                assert isinstance(args.starttime, obspy.core.AttribDict)

        # This is now a bit of a modified clone of _get_seismogram_times()
        # of the base instaseis database object. It is modified as the
        # finite sources are a bit different.
        db = self.application.db

        time_of_first_sample = args.origintime - finite_source.time_shift

        # This is guaranteed to be exactly on a sample due to the previous
        # calculations.
        earliest_starttime = time_of_first_sample + \
            finite_source.additional_time_shift
        latest_endtime = time_of_first_sample + db.info.length

        if args.dt is not None and round(args.dt / db.info.dt, 6) != 0:
            affected_area = args.kernelwidth * db.info.dt
            latest_endtime -= affected_area

        # If the endtime is not set, do it here.
        if args.endtime is None:
            args.endtime = latest_endtime

        # Do a couple of sanity checks here.
        if isinstance(args.starttime, obspy.UTCDateTime):
            # The desired seismogram start time must be before the end time of
            # the seismograms.
            if args.starttime >= latest_endtime:
                msg = ("The `starttime` must be before the seismogram ends.")
                raise tornado.web.HTTPError(400, log_message=msg, reason=msg)
            # Arbitrary limit: The starttime can be at max one hour before the
            # origin time.
            if args.starttime < (earliest_starttime - 3600):
                msg = ("The seismogram can start at the maximum one hour "
                       "before the origin time.")
                raise tornado.web.HTTPError(400, log_message=msg, reason=msg)

        if isinstance(args.endtime, obspy.UTCDateTime):
            # The endtime must be within the seismogram window
            if not (earliest_starttime <= args.endtime <= latest_endtime):
                msg = ("The end time of the seismograms lies outside the "
                       "allowed range.")
                raise tornado.web.HTTPError(400, log_message=msg, reason=msg)

        return time_of_first_sample, earliest_starttime, latest_endtime

    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def post(self):
        # Parse the arguments. This will also perform a number of sanity
        # checks.
        args = self.parse_arguments()
        self.set_headers(args)

        # Coroutine + thread as potentially pretty expensive.
        response = yield tornado.gen.Task(
            _parse_and_resample_finite_source,
            request=self.request, db_info=self.application.db.info)

        # If an exception is returned from the task, re-raise it here.
        if isinstance(response, Exception):
            raise response

        finite_source = response

        time_of_first_sample, min_starttime, max_endtime = \
            self.parse_time_settings(args, finite_source=finite_source)

        # Generating even 100'000 receivers only takes ~150ms so its totally
        # ok to generate them all at once here. The time to generate and
        # send the seismograms will dominate.
        receivers = self.get_receivers(args)

        # If a zip file is requested, initialize it here and write to custom
        # buffer object.
        if args.format == "saczip":
            buf = IOQueue()
            zip_file = zipfile.ZipFile(buf, mode="w")

        # Count the number of successful extractions. Phase relative offsets
        # could result in no actually calculated seismograms. In that case
        # we would like to raise an error.
        count = 0

        # Loop over each receiver, get the synthetics and stream it to the
        # user.
        for receiver in receivers:

            # Check if the connection is still open. The connection_closed
            # flag is set by the on_connection_close() method. This is
            # pretty manual right now. Maybe there is a better way? This
            # enables to server to stop serving if the connection has been
            # cancelled on the client side.
            if self.connection_closed:
                self.flush()
                self.finish()
                return

            # Check if start- or end time are phase relative. If yes
            # calculate the new start- and/or end time.
            time_values = self.get_phase_relative_times(
                args=args, source=finite_source, receiver=receiver,
                min_starttime=min_starttime, max_endtime=max_endtime)
            if time_values is None:
                continue
            starttime, endtime = time_values

            # Validate the source-receiver geometry.
            # self.validate_geometry(source=finite_source, receiver=receiver)

            # Yield from the task. This enables a context switch and thus
            # async behaviour.
            response = yield tornado.gen.Task(
                _get_finite_source,
                db=self.application.db, finite_source=finite_source,
                receiver=receiver, components=list(args.components),
                units=args.units, dt=args.dt, kernelwidth=args.kernelwidth,
                starttime=starttime, endtime=endtime,
                time_of_first_sample=time_of_first_sample, format=args.format,
                label=args.label)

            # Check connection once again.
            if self.connection_closed:
                self.flush()
                self.finish()
                return

            # If an exception is returned from the task, re-raise it here.
            if isinstance(response, Exception):
                raise response
            # It might return a list, in that case each item is a bytestring
            # of SAC file.
            elif isinstance(response, list):
                assert args.format == "saczip"
                for filename, content in response:
                    zip_file.writestr(filename, content)
                for data in buf:
                    self.write(data)
            # Otherwise it contain MiniSEED which can just directly be
            # streamed.
            else:
                self.write(response)
            self.flush()

            count += 1

        # If nothing is written, raise an error. This should really only
        # happen with phase relative offsets with phases not coinciding with
        # the source - receiver geometry.
        if not count:
            msg = ("No seismograms found for the given phase relative "
                   "offsets. This could either be due to the chosen phase "
                   "not existing for the specific source-receiver geometry "
                   "or arriving too late/with too large offsets if the "
                   "database is not long enough.")
            raise tornado.web.HTTPError(400, log_message=msg, reason=msg)

        # Write the end of the zipfile in case necessary.
        if args.format == "saczip":
            zip_file.close()
            for data in buf:
                self.write(data)

        self.finish()
