"""
This script was tested with Python2.7.14, because the core dependency
(pyasdf) is not yet (04/02/2018) stable under python3.
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
import signal
import sys
import time

WRITER_RANK = 0
OUTPUT_BLOCK_SIZE = 1000

PROCESSOR_NAME = MPI.Get_processor_name()
COMM = MPI.COMM_WORLD
RANK, SIZE = COMM.Get_rank(), COMM.Get_size()

logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("wfs_in",
                        type=str,
                        help="input ASDF waveform dataset.")
    parser.add_argument("events_in",
                        type=str,
                        help="input event/phase data HDFStore")
    parser.add_argument("config_file",
                        type=str,
                        help="configuration file")
    parser.add_argument("-o", "--outfile",
                        type=str,
                        default="corr.h5",
                        help="output HDF5 file for correlation results")
    parser.add_argument("-c", "--control",
                        type=str,
                        help="HDF5 control file with events to correlate")
    parser.add_argument("-l", "--logfile",
                        type=str,
                        help="log file")
    parser.add_argument("-v", "--verbose",
                        action="store_true",
                        help="verbose")
    args = parser.parse_args()
    return(args)

def main(args, cfg):

    logger.info("starting process - rank %d" % RANK)

    logger.info("loading event and phase data ")
    df0_event, df0_phase = load_event_data(args.events_in)
    logger.info("event and phase data loaded")

    if args.control is not None:
        logger.info("using control file %s" % args.control)
        with pd.HDFStore(args.control, mode="r") as control:
            control_list = control["events"]
            df0_event = df0_event[df0_event.index.isin(control_list)]
    else:
        control_list = df0_event.index

    if RANK == WRITER_RANK:
# Send assignment to each worker-rank.
        for _rank, _data in zip([i for i in range(SIZE) if i != WRITER_RANK],
                                np.array_split(control_list, SIZE-1)):
            COMM.send(_data, _rank)
# Enter the output loop and exit at the end.
        with h5py.File(args.outfile, "w") as f5:
            initialize_output(f5)
            write_loop(f5)
        exit()

# Configure the HDF5 cache.
    cache_config = (cfg["cache_mdc"],
                    cfg["cache_rdcc"],
                    cfg["cache_rdcc_nbytes"],
                    cfg["cache_rdcc_w0"])
    logger.info("setting HDF5 cache configuration - (%d, %d, %d, %.2f)" % cache_config)
    propfaid = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
    propfaid.set_cache(*cache_config)
    try:
        fid = h5py.h5f.open(args.wfs_in,
                            flags=h5py.h5f.ACC_RDONLY,
                            fapl=propfaid)
        with h5py.File(fid, mode="r", driver="mpio", comm=COMM) as asdf_h5:
            logger.info("receiving scattered data")
            data = COMM.recv()

            for evid in data:
                logger.debug("correlating %d" % evid)
                try:
                    correlate(evid, asdf_h5, df0_event, df0_phase, cfg)
                except Exception as err:
                    logger.error(err)
            logger.info("successfully completed correlation")
    except Exception as err:
        logger.error(err)
        fid.close()
    finally:
# Send a signal to the writer-rank that this worker-rank has finished.
        COMM.send(StopIteration, WRITER_RANK)

def parse_config(config_file):
    parser = configparser.ConfigParser()
    parser.readfp(open(config_file))
    config = {"tlead_p"           : parser.getfloat("general", "tlead_p"),
              "tlead_s"           : parser.getfloat("general", "tlead_s"),
              "tlag_p"            : parser.getfloat("general", "tlag_p"),
              "tlag_s"            : parser.getfloat("general", "tlag_s"),
              "corr_min"          : parser.getfloat("general", "corr_min"),
              "knn"               : parser.getint(  "general", "knn"),
              "filter_fmin"       : parser.getfloat("filter",  "freq_min"),
              "filter_fmax"       : parser.getfloat("filter",  "freq_max"),
              "cache_mdc"         : parser.getint(  "hdf5-cache", "mdc"),
              "cache_rdcc"        : parser.getint(  "hdf5-cache", "rdcc"),
              "cache_rdcc_nbytes" : parser.getint(  "hdf5-cache", "rdcc_nbytes"),
              "cache_rdcc_w0"     : parser.getfloat("hdf5-cache", "rdcc_w0")}
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
                    "%(funcName)s()::%(lineno)d::{:s}::{:04d}:: "\
                    "%(message)s".format(PROCESSOR_NAME, RANK),
                    datefmt="%Y%jT%H:%M:%S")
        else:
            formatter = logging.Formatter(fmt="%(asctime)s::%(levelname)s::"\
                    "{:s}::{:04d}:: %(message)s".format(PROCESSOR_NAME, RANK),
                    datefmt="%Y%jT%H:%M:%S")
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
    with pd.HDFStore(f5in, mode="r") as cat:
        if evids is None:
            return(cat["event"], cat["phase"])
        else:
            return(cat["event"].loc[evids], cat["phase"].loc[evids])

def get_waveforms_for_reference(asdf_h5, ref, net, sta):
    _st = op.Stream()
    _label = "/".join(("/References", ref, net, sta))
    for _loc in asdf_h5[_label]:
        for _chan in asdf_h5["/".join((_label, _loc))]:
            _handle = "/".join((_label, _loc, _chan))
            _ref = asdf_h5[_handle]
            _ds = asdf_h5[_ref.attrs["reference_path"]]
            _tr                  = op.Trace(data=_ds[_ref[0]: _ref[1]])
            _tr.stats.delta      = 1./_ds.attrs["sampling_rate"]
            _tr.stats.starttime  = op.UTCDateTime(_ref.attrs["starttime"]*1e-9)
            _tr.stats.network    = net
            _tr.stats.station    = sta
            _tr.stats.location   = _loc if _loc != "__" else ""
            _tr.stats.channel    = _chan
            _st.append(_tr)
    return(_st)

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

def correlate(evid, asdf_h5, df0_event, df0_phase, cfg):
    """
    Correlate an event with its K nearest-neighbours.

    Arguments:
    evid      :: int
                 The event ID of the "control" or "template" event.
    asdf_h5 :: pyasdf.ASDFDataSet
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
    COMM = MPI.COMM_WORLD
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
        _ncorr_a, _ncorr_s = 0, 0
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
                __t = time.time()
                st0 = get_waveforms_for_reference(asdf_h5,
                                                  "event%d" % evid0,
                                                  arrival["net"],
                                                  arrival["sta"])
                logger.debug("waveform retrieval took %.5f seconds" % (time.time()-__t))
                __t = time.time()
                stB = get_waveforms_for_reference(asdf_h5,
                                                  "event%d" % evidB,
                                                  arrival["net"],
                                                  arrival["sta"])
                logger.debug("waveform retrieval took %.5f seconds" % (time.time()-__t))
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
            try:
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
                    atY = otY + ttX
                    # filter the traces
                    trX = trX.filter("bandpass",
                                     freqmin=cfg["filter_fmin"],
                                     freqmax=cfg["filter_fmax"])
                    trY = trY.filter("bandpass",
                                     freqmin=cfg["filter_fmin"],
                                     freqmax=cfg["filter_fmax"])
                    # slice the template trace
                    trX = trX.slice(starttime=atX-cfg["tlead_%s" % arrival["phase"].lower()],
                                    endtime  =atX+cfg["tlag_%s" % arrival["phase"].lower()])
                    # slice the test trace
                    trY = trY.slice(starttime=atY-cfg["tlead_%s" % arrival["phase"].lower()],
                                    endtime  =atY+cfg["tlag_%s" % arrival["phase"].lower()])
                    # error checking
                    min_nsamp = int(
                            (cfg["tlead_%s" % arrival["phase"].lower()]\
                           + cfg["tlag_%s" % arrival["phase"].lower()]) \
                           * trX.stats.sampling_rate
                           )
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
                    __t = time.time()
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
                    _ddiff = tshift
                    logger.debug("correlation tooks %.5f seconds" % (time.time() - __t))
                    _ncorr_a += 1
                    # store values if the correlation is high
                    if abs(_ccmax) >= cfg["corr_min"]:
                    #if _ccmax >= cfg["corr_min"]:
                        _ncorr_s += 1
                        ddiff.append(_ddiff)
                        ccmax.append(_ccmax)
            finally:
                # if cross-correlation was successful, output best value
                if len(ddiff) > 0:
                    grpid = "{:d}/{:d}/{:s}".format(evid0,
                                                    evidB,
                                                    arrival["sta"])
                    dsid = "{:s}/{:s}".format(grpid,
                                              arrival["phase"])
                    idxmax = np.argmax(np.abs(ccmax))
                    ddiff = ddiff[idxmax]
                    ccmax = ccmax[idxmax]
                    logger.debug("{:s}: {:.2f}, {:.2f}".format(dsid,
                                                               ddiff,
                                                               ccmax))
                    data = {"grpid": grpid,
                            "dsid" : dsid,
                            "ddiff": ddiff,
                            "ccmax": ccmax,
                            "chan" : arrival["chan"],
                            "phase": arrival["phase"]}
                else:
                    data = None

                __t = time.time()
                COMM.send(data, WRITER_RANK)
                logger.debug("writing took %.5f seconds" % (time.time() - __t))

        logger.info("correlated event ID#{:d} with ID#{:d} - elapsed time: "\
                    "{:6.2f} s, ncorr = ({:d}/{:d})".format(evid0,
                                                            evidB,
                                                            time.time()-log_tstart,
                                                            _ncorr_s,
                                                            _ncorr_a))

def initialize_output(f5):
    f5.create_dataset("evidA", (0,), maxshape=(None,), dtype="i")
    f5.create_dataset("evidB", (0,), maxshape=(None,), dtype="i")
    f5.create_dataset("sta", (0,), maxshape=(None,), dtype="S5")
    f5.create_dataset("chan", (0,), maxshape=(None,), dtype="S6")
    f5.create_dataset("phase", (0,), maxshape=(None,), dtype="S1")
    f5.create_dataset("ddiff", (0,), maxshape=(None,), dtype="f")
    f5.create_dataset("ccmax", (0,), maxshape=(None,), dtype="f")

def write_loop(f5):
    stop_count = 0
    idx = 0
    keys = ("evidA", "evidB", "sta", "phase", "chan", "ddiff", "ccmax")
    while True:
        if stop_count == SIZE-1:
            return
        data = COMM.recv()
        if data is None:
            continue
        elif data is StopIteration:
            stop_count += 1
            continue
        else:
            if idx % OUTPUT_BLOCK_SIZE == 0:
                for key in keys:
                    f5[key].resize((f5[key].shape[0] + OUTPUT_BLOCK_SIZE,))
            evidA, evidB, sta, phase = data["dsid"].split("/")
            values = [int(evidA),
                      int(evidB),
                      sta.encode(),
                      phase.encode(),
                      data["chan"].encode(),
                      data["ddiff"],
                      data["ccmax"]]
            for key, value in zip(keys, values):
                f5[key][idx] = value
            idx += 1

def detect_python_version():
    if sys.version_info.major != 2:
        logger.error("Python2 is currently the only supported version of this"
                     "code. Please use a Python2 interpreter.")
        exit()

def signal_handler(sig, frame):
    raise(SystemError("Interrupting signal received... aborting"))

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGCONT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

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
