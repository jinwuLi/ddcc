"""
This script was tested with Python2.7.14, because the core dependency
(pyasdf) is not yet (04/02/2018) stable under python3.

TODO:: logging
"""
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

import argparse
import ConfigParser as configparser
import h5py
import logging
import mpi4py.MPI as MPI
import numpy as np
import obspy as op
import obspy.signal.cross_correlation
import os
import pandas as pd
import pyasdf
import sys
import time
import traceback

logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("wfs_in",      type=str,
                                       nargs="?",
                                       help="input ASDF waveform dataset.")
    parser.add_argument("events_in",   type=str,
                                       help="input event/phase data "\
                                                      "HDFStore")
    parser.add_argument("config_file", type=str,
                                       help="configuration file")
    parser.add_argument("-i", "--init_corr", action="store_true",
                                             help="initialize correlation "
                                                  "output file")
    parser.add_argument("-a", "--append_corr", action="store_true",
                                               help="initialize correlation "
                                                    "output file")
    parser.add_argument("-o", "--outfile",   type=str,
                                             default="corr.h5",
                                             help="output HDF5 file for "
                                                  "correlation results")
    parser.add_argument("-l", "--logfile",   type=str,
                                             help="log file")
    parser.add_argument("-v", "--verbose",   action="store_true",
                                             help="verbose")
    args = parser.parse_args()

    if args.init_corr is not True and not os.path.isfile(args.outfile):
        print("Output file does not exist. Use -i option to intialize "
              "it.")
        exit()
    elif args.init_corr is True:
        print("Initializing output file - %s" % args.outfile)
    elif args.append_corr is True:
        print("Appending to output file - %s" % args.outfile)
    else:
        print("Skipping output file initialization")

    args.correlate = True

    if args.wfs_in is None:
        print("No waveform file specified. Not correlating.")
        args.correlate = False
    return(args)

def main(args, cfg):

    comm = MPI.COMM_WORLD
    rank, size = comm.Get_rank(), comm.Get_size()

    logger.info("starting process - rank %d" % rank)

    if args.init_corr:
        with h5py.File(args.outfile, "w", driver="mpio", comm=comm) as f5out:
            logger.info("initializing output file")
            initialize_f5out(f5out, args, cfg)
    elif args.append_corr:
        logger.debug(args.outfile)
        with h5py.File(args.outfile, "a", driver="mpio", comm=comm) as f5out:
            logger.info("appending to output file")
            append_f5out(f5out, args, cfg)

    if args.correlate is True:
        logger.info("beginning to correlate")
    else:
        logger.info("not correlating")
        exit()

    with pyasdf.ASDFDataSet(args.wfs_in, mode="r") as asdf_dset:

        logger.info("loading events to scatter")
        df0_event, df0_phase = load_event_data(args.events_in)
        logger.info("events loaded")

        if rank == 0:
            data = np.array_split(df0_event.index, size)
        else:
            data = None
        logger.info("receiving scattered data")
        data = comm.scatter(data, root=0)

        with h5py.File(args.outfile, "a", driver="mpio", comm=comm) as f5out:
            for evid in data:
                try:
                    correlate(evid, asdf_dset, f5out, df0_event, df0_phase, cfg)
                except Exception as err:
                    logger.error(err)
    logger.info("successfully completed correlation")

def parse_config(config_file):
    parser = configparser.ConfigParser()
    parser.readfp(open(config_file))
    config = {"tlead_p"  : parser.getfloat("general", "tlead_p"),
              "tlead_s"  : parser.getfloat("general", "tlead_s"),
              "tlag_p"   : parser.getfloat("general", "tlag_p"),
              "tlag_s"   : parser.getfloat("general", "tlag_s"),
              "corr_min" : parser.getfloat("general", "corr_min"),
              "knn"      : parser.getint(  "general", "knn")}
    return(config)


def configure_logging(verbose, logfile):
    """
    A utility function to configure logging.
    """
    if verbose is True:
        level = logging.DEBUG
    else:
        level = logging.INFO
    for name in (__name__,):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if level == logging.DEBUG:
            formatter = logging.Formatter(fmt="%(asctime)s::%(levelname)s::"\
                    "%(funcName)s()::%(lineno)d::%(process)d:: %(message)s",
                    datefmt="%Y%j %H:%M:%S")
        else:
            formatter = logging.Formatter(fmt="%(asctime)s::%(levelname)s::"\
                    " %(message)s",
                    datefmt="%Y%j %H:%M:%S")
        if logfile is not None:
            file_handler = logging.FileHandler(logfile)
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

def load_event_data(f5in, evids=None):
    with pd.HDFStore(f5in) as cat:
        if evids is None:
            return(cat["event"], cat["phase"])
        else:
            return(cat["event"].loc[evids], cat["phase"].loc[evids])

def get_knn(evid, df_event, k=10):
    """
    Get the K nearest-neighbour events.
    Only returns events with IDs greater than evid.

    evid :: event ID of primary event.
    k    :: number of nearest-neighbours to retrieve.
    """
    lat0, lon0, depth0, time0 = df_event.loc[evid][["lat", "lon", "depth", "time"]]
    _df = df_event.copy()
    _df = _df[_df.index >= evid]
    _df["distance"] = np.sqrt(
        (np.square(_df["lat"]-lat0) + np.square(_df["lon"]-lon0))*111.11**2
        +np.square(_df["depth"]-depth0)
    )
    return(_df.sort_values("distance").iloc[:k+1])

def get_phases(evids, df_phase):
    """
    Get phase data for a set of events.

    evids :: list of event IDs to retrieve phase data for.
    """
    return(
        df_phase[
            df_phase.index.isin(evids)
        ].sort_index(
        ).sort_values(["sta", "phase"])
    )

def append_f5out(f5out, args, cfg):
    """
    Append metadata structure for output HDF5 file.

    This is a collective operation.
    """
    with pd.HDFStore(args.events_in, "r") as f5in:
        df0_event = f5in["event"]
        df0_phase = f5in["phase"]
        for evid0 in df0_event.index:
            logger.info("initializing output for {:d}".format(evid0))
            for evidB in get_knn(evid0, df0_event, k=cfg["knn"]).iloc[1:].index:
                df_phase = get_phases(
                    (evid0, evidB),
                    df0_phase
                ).drop_duplicates(
                    ["sta", "phase"]
                )
                for _, arrival in df_phase.iterrows():
                    dsid = "{:d}/{:d}/{:s}/{:s}".format(evid0,
                                                        evidB,
                                                        arrival["sta"],
                                                        arrival["phase"])
                    if dsid not in f5out:
                        dset = f5out.create_dataset(dsid,
                                                    (2,),
                                                    dtype="f",
                                                    fillvalue=np.nan)
                        dset.attrs["chan"] = arrival["chan"]
                        logger.debug(dsid)
                    else:
                        logger.debug("{:s} already exists".format(dsid))

def initialize_f5out(f5out, args, cfg):
    """
    Initialize metadata structure for output HDF5 file.

    This is a collective operation.
    """
    with pd.HDFStore(args.events_in, "r") as f5in:
        df0_event = f5in["event"]
        df0_phase = f5in["phase"]
        for evid0 in df0_event.index:
            for evidB in get_knn(evid0, df0_event, k=cfg["knn"]).iloc[1:].index:
                df_phase = get_phases(
                    (evid0, evidB),
                    df0_phase
                ).drop_duplicates(
                    ["sta", "phase"]
                )
                for _, arrival in df_phase.iterrows():
                    grpid = "{:d}/{:d}/{:s}".format(evid0, evidB, arrival["sta"])
                    if grpid not in f5out:
                        grp = f5out.create_group(grpid)
                    else:
                        grp = f5out[grpid]
                    logger.debug("{:s}/{:s}".format(grpid,
                                                    arrival["phase"]))
                    dset = grp.create_dataset(arrival["phase"],
                                              (2,),
                                              dtype="f",
                                              fillvalue=np.nan)
                    dset.attrs["chan"] = arrival["chan"]

def correlate(evid, asdf_dset, f5out, df0_event, df0_phase, cfg):
    """
    Correlate an event with its K nearest-neighbours.

    Arguments:
    evid      :: int
                 The event ID of the "control" or "template" event.
    asdf_dset :: pyasdf.ASDFDataSet
                 The waveform dataset. It is assumed that each waveform
                 is given a tag "event$EVID" where $EVID is the event ID
                 of the associated event. This tag format may change in
                 the future.
    f5out     :: h5py.File
                 The output data file where results will be stored. This
                 file needs to be initialized with the proper metadata
                 structure; this can be achieved with initialize_f5out().
    df0_event :: pandas.DataFrame
                 A DataFrame of all events in the dataset. The DataFrame
                 must be indexed by event ID and the columns must be
                 lat, lon, depth, and time.
    df0_phase :: pandas.DataFrame
                 A DataFrame of all phase information in the dataset. The
                 DataFrame must be indexed by event ID and the columns must
                 be arid, sta, chan, phase, time, prefor, and net.

    Returns:
    None
    """
    # df_event :: DataFrame of K-nearest-neighbour events including
    #             primary event.
    # event0   :: primary event
    # evid0    :: primary event ID
    df_event = get_knn(evid, df0_event, k=cfg["knn"])
    df_phase = get_phases(df_event.index, df0_phase)
    event0 = df_event.iloc[0]
    evid0 = event0.name
    for evidB, eventB in df_event.iloc[1:].iterrows():
        # log_tstart :: for logging elapsed time
        # ot0        :: origin time of the primary event
        # otB        :: origin time of the secondary event
        # _df_phase  :: DataFrame with arrival data for the primary and
        #               secondary events
        # __df_phase ::
        log_tstart = time.time()
        ot0 = op.core.UTCDateTime(event0["time"])
        otB = op.core.UTCDateTime(eventB["time"])

        _df_phase = get_phases((evid0, evidB), df_phase=df_phase)
        __df_phase = _df_phase.drop_duplicates(["sta", "phase"])

        for _, arrival in __df_phase.iterrows():
            # ddiff   :: array of double-difference measurements for
            #            this station:phase pair
            # ccmax   :: array of maximum cross-correlation
            #            coefficients for this station:phase pair
            # shift   :: for plotting
            # st0     :: waveform Stream for primary event
            # stB     :: waveform Stream for secondary event
            ddiff, ccmax = [], []
            try:
                st0 = asdf_dset.waveforms["%s.%s" % (arrival["net"],
                                                     arrival["sta"])]["event%d" % evid0]
                stB = asdf_dset.waveforms["%s.%s" % (arrival["net"],
                                                     arrival["sta"])]["event%d" % evidB]
            except KeyError as err:
                continue
            # tr0 :: waveform Trace for primary event
            # trB :: waveform Trace for secondary event
            # trX :: "template" trace; this is ideally the primary event Trace,
            #        but the secondary event Trace will be used if the only
            #        arrival for this station:phase pair comes from the secondary
            #        event
            # trY :: "test" trace; this is ideally the secondary event Trace
            # atX :: arrival-time of the template arrival
            # otY :: origin-time of the "test" event
            for tr0 in st0:
                try:
                    trB = stB.select(channel=tr0.stats.channel)[0]
                except IndexError as err:
                    continue
                atX = op.core.UTCDateTime(arrival["time"])
                if arrival.name == evid0:
                # Do the calculation "forward".
                # This means that the primary (earlier) event is used as the template
                # trace.
                    trX, trY     = tr0, trB
                    otX, otY     = ot0, otB
                    evidX, evidY = evid0, evidB
                else:
                # Do the calculation "backward".
                # This means that the secondary (later) event is used as the template
                # trace.
                    trX, trY     = trB, tr0
                    otX, otY     = otB, ot0
                    evidX, evidY = evidB, evid0
                ttX = atX - otX
                # Get the arrival time for the test trace. Use the
                # from the database if one exists, otherwise do
                # a simple arrival time prediction.
                #if evidY in _df_phase.index\
                #        and np.any((_df_phase.loc[evidY]["sta"]    == arrival["sta"])\
                #                  &(_df_phase.loc[evidY]["phase"] == arrival["phase"])):
                #    _df = _df_phase.loc[evidY]
                #    _arrival = _df[(_df["sta"] == arrival["sta"])
                #                  &(_df["phase"] == arrival["phase"])].iloc[0]
                #    atY = op.core.UTCDateTime(_arrival["time"])
                #else:
                #    wavespeed = cfg["vp"] if arrival["phase"] == "P" else cfg["vs"]
                #    # This is the wrong distance.
                #    atY = otY + arrival["dist"]/wavespeed
                atY = otY + ttX
                # slice the template trace
                trX = trX.slice(starttime=atX-cfg["tlead_%s" % arrival["phase"].lower()],
                                endtime  =atX+cfg["tlag_%s" % arrival["phase"].lower()])
                # slice the test trace
                trY = trY.slice(starttime=atY-cfg["tlead_%s" % arrival["phase"].lower()],
                                endtime  =atY+cfg["tlag_%s" % arrival["phase"].lower()])
                # error checking
                min_nsamp = (cfg["tlead_%s" % arrival["phase"].lower()]\
                           + cfg["tlag_%s" % arrival["phase"].lower()]) * trX.stats.sampling_rate
                if len(trX) < min_nsamp or len(trY) < min_nsamp:
                    logger.debug("len(trX), len(trY), min_nsamp: "\
                                 "{:d}, {:d}, {:d}".format(len(trX),
                                                           len(trY),
                                                           min_nsamp))
                    continue

                # max shift :: the maximum shift to apply when cross-correlating
                # corr      :: the cross-correlation time-series
                # clag      :: the lag of the maximum cross-correlation
                #              coefficient. 0 shift corresponds to the
                #              case below where both traces are center-
                #              aligned
                #          ---------|+++++++++
                #          9876543210123456789
                #     trX: -------XXXXX-------
                #     trY: YYYYYYYYYYYYYYYYYYY
                # _ccmax    :: the maximum cross-correlation coefficient
                # tshift    :: clag converted to units of time
                # t0X       :: the center time of trX
                # t0Y       :: the center time of trY
                ## iet      :: inter-event time
                ## iat      :: inter-arrival time
                ## _ddiff   :: double-difference (differential travel-time)
                # Do the actual correlation
                max_shift     = int(len(trY)/2)
                corr          = op.signal.cross_correlation.correlate(trY,
                                                                      trX,
                                                                      max_shift)
                clag, _ccmax  = op.signal.cross_correlation.xcorr_max(corr)
                tshift        = clag * trY.stats.delta
                t0X           = np.mean([trX.stats.starttime.timestamp,
                                         trX.stats.endtime.timestamp])
                t0Y           = np.mean([trY.stats.starttime.timestamp,
                                         trY.stats.endtime.timestamp])
                #iet           = (otY-otX)
                #iat           = (t0Y-t0X+tshift)
                _ddiff = tshift
                # store values if the correlation is high
                if abs(_ccmax) >= cfg["corr_min"]:
                #if _ccmax >= cfg["corr_min"]:
                    ddiff.append(_ddiff)
                    ccmax.append(_ccmax)
            # if cross-correlation was successful, output best value
            if len(ddiff) > 0:
                grpid = "{:d}/{:d}/{:s}".format(evid0, evidB, arrival["sta"])
                idxmax = np.argmax(np.abs(ccmax))
                ddiff = ddiff[idxmax]
                ccmax = ccmax[idxmax]
                logger.debug("{:s}/{:s}: {:.2f}, {:.2f}".format(grpid,
                                                                arrival["phase"],
                                                                ddiff,
                                                                ccmax))
                try:
                    f5out[grpid][arrival["phase"]][:] = (ddiff, ccmax)
                except Exception as err:
                    logger.error(err)
                    raise
        logger.info("correlated event ID#{:d} with ID#{:d} - elapsed time: "\
              "{:.2f} s".format(evid0, evidB, time.time()-log_tstart))

def detect_python_version():
    if sys.version_info.major != 2:
        logger.error("Python2 is currently the only supported version of this"
                     "code. Please use a Python2 interpreter.")
        exit()

if __name__ == "__main__":
    args = parse_args()
    cfg = parse_config(args.config_file)
    configure_logging(args.verbose, args.logfile)
    detect_python_version()
    try:
        main(args, cfg)
    except Exception as err:
        logger.error("I died")
        logger.error(err)
