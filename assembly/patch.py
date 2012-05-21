#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Patch the sequences of one assembly using sequences from another assembly. This
is tested on merging the medicago WGS assembly with the clone-by-clone assembly.
"""

import os.path as op
import sys
import logging

from itertools import groupby
from optparse import OptionParser

from jcvi.formats.bed import Bed
from jcvi.formats.fasta import Fasta
from jcvi.utils.range import range_parse
from jcvi.formats.base import must_open
from jcvi.apps.base import ActionDispatcher, debug, sh, mkdir
debug()


def main():

    actions = (
        ('mapbreaks', 'find scaffold breakpoints using genetic map'),
        ('refine', 'find gaps within or near breakpoint regions'),
        ('prepare', 'given om alignment, prepare the patchers'),
        ('certificate', 'generate pairwise overlaps'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


OK, BREAK, END = range(3)

def check_markers(a, b, maxdiff):
    from jcvi.algorithms.matrix import hamming_distance

    aid, ageno = a[0], a[1:]
    bid, bgeno = b[0], b[1:]
    ascf, apos = aid.split(".")
    bscf, bpos = bid.split(".")
    if ascf != bscf:
        return END, None
    diff = hamming_distance(ageno, bgeno, ignore="-")
    max_allowed = len(ageno) * maxdiff
    if diff <= max_allowed:
        return OK, None

    return BREAK, (ascf, apos, bpos)


def mapbreaks(args):
    """
    %prog mapbreaks mstmap.input > breakpoints.bed

    Find scaffold breakpoints using genetic map. Use formats.vcf.mstmap() to
    generate the input for this routine.
    """
    from jcvi.utils.iter import pairwise

    p = OptionParser(mapbreaks.__doc__)
    p.add_option("--diff", default=.1, type="float",
                 help="Maximum ratio of differences allowed [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    mstmap, = args
    diff = opts.diff
    fp = open(mstmap)
    data = []
    for row in fp:
        if row.startswith("locus_name"):
            break

    for row in fp:
        data.append(row.split())

    # Remove singleton markers (avoid double cross-over)
    good = []
    nsingletons = 0
    for i in xrange(1, len(data) - 1):
        a = data[i]
        left_label, left_rr = check_markers(data[i - 1], a, diff)
        right_label, right_rr = check_markers(a, data[i + 1], diff)

        if left_label == BREAK and right_label == BREAK:
            nsingletons += 1
            continue

        good.append(a)

    logging.debug("A total of {0} singleton markers removed.".format(nsingletons))

    for a, b in pairwise(good):
        label, rr = check_markers(a, b, diff)
        if label == BREAK:
            print "\t".join(rr)


def refine(args):
    """
    %prog refine breakpoints.bed gaps.bed

    Find gaps within or near breakpoint region.
    """
    from jcvi.formats.base import FileMerger

    p = OptionParser(refine.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    breakpointsbed, gapsbed = args
    cmd = "intersectBed -wao -a {0} -b {1}".format(breakpointsbed, gapsbed)

    pf = "{0}.{1}".format(breakpointsbed.split(".")[0], gapsbed.split(".")[0])
    ingapsbed = pf + ".bed"
    sh(cmd, outfile=ingapsbed)

    fp = open(ingapsbed)
    data = [x.split() for x in fp]
    nogapsbed = pf + ".nogaps.bed"
    largestgapsbed = pf + ".largestgaps.bed"
    nogapsfw = open(nogapsbed, "w")
    largestgapsfw = open(largestgapsbed, "w")
    for b, gaps in groupby(data, key=lambda x: x[:3]):
        gaps = list(gaps)
        if len(gaps) == 1 and gaps[0][3] == ".":
            gap = gaps[0]
            assert gap[4] == "-1"
            print >> nogapsfw, "\t".join(b)
            continue

        gaps = [(int(x[-1]), x) for x in gaps]
        maxgap = max(gaps)[1]
        print >> largestgapsfw, "\t".join(maxgap[3:])

    nogapsfw.close()
    largestgapsfw.close()

    closestgapsbed = pf + ".closestgaps.bed"
    closestgapsfw = open(closestgapsbed, "w")
    cmd = "closestBed -a {0} -b {1}".format(nogapsbed, gapsbed)
    cmd += " | cut -f4-7"
    sh(cmd, outfile=closestgapsbed)

    refinedbed = pf + ".refined.bed"
    FileMerger([largestgapsbed, closestgapsbed], outfile=refinedbed).merge()

    # Clean-up
    cmd = "rm -f " + " ".join([nogapsbed, largestgapsbed, closestgapsbed])
    sh(cmd)


def phase(aid, fastadir=None, backbone=None):
    af = op.join(fastadir, aid + ".fasta")
    f = Fasta(af)
    k, fs = f.itersizes().next()
    assert aid == k

    ph = 2 if aid.startswith(backbone) else 1
    return ph, fs


def certificate(args):
    """
    %prog certificate tpffile certificatefile fastafile

    North  chr1  2  0  AC229737.8  telomere     58443
    South  chr1  2  1  AC229737.8  AC202463.29  58443  37835  58443  + Non-terminal

    Each line describes a relationship between the current BAC and the
    north/south BAC. First, "North/South" tag, then the chromosome, phases of
    the two BACs, ids of the two BACs, the size and the overlap start-stop of
    the CURRENT BAC, and orientation. Each BAC will have two lines in the
    certificate file.

    Here we use BAC to refer to sequence ranges. BAC phase are priority, and
    determines how the overlaps are called.
    """
    from jcvi.assembly.goldenpath import TPF, check_certificate, overlap

    p = OptionParser(certificate.__doc__)
    p.add_option("--backbone", default="Scaffold",
                 help="Prefix of the backbone assembly [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    tpffile, certificatefile, fastafile = args
    bb = opts.backbone

    fastadir = "fasta"
    mkdir(fastadir)
    cmd = "faSplit byname {0} {1}/".format(fastafile, fastadir)
    sh(cmd)

    cmd = "rename .fa .fasta {0}/*.fa".format(fastadir)
    sh(cmd)

    tpf = TPF(tpffile)

    data = check_certificate(certificatefile)
    fw = must_open(certificatefile, "w")
    for i, a in enumerate(tpf):
        if a.is_gap:
            continue

        aid = a.component_id

        af = op.join(fastadir, aid + ".fasta")
        assert op.exists(af), "ID `{0}` not found".format(aid)

        north, south = tpf.getNorthSouthClone(i)
        aphase, asize = phase(aid, fastadir=fastadir, backbone=bb)

        for tag, p in (("North", north), ("South", south)):
            if not p:  # end of the chromosome
                ov = "telomere\t{0}".format(asize)
            elif p.isCloneGap:
                bphase = "0"
                ov = "{0}\t{1}".format(p.gap_type, asize)
            else:
                bid = p.component_id
                bphase, bsize = phase(bid, fastadir=fastadir, backbone=bb)
                key = (tag, aid, bid)
                if key in data:
                    print >> fw,  data[key]
                    continue

                ar = [aid, bid, "--dir=" + fastadir]
                o = overlap(ar)
                ov = o.certificateline if o \
                        else "{0}\t{1}\tNone".format(bid, asize)

            print >> fw, "\t".join(str(x) for x in \
                    (tag, a.object, aphase, bphase, aid, ov))
            fw.flush()


def merge_ranges(beds):

    m = [x.accn for x in beds]

    mr = [range_parse(x) for x in m]
    mc = set(x.seqid for x in mr)
    if len(mc) != 1:
        logging.error("Multiple seqid found in pocket. Aborted.")
        return

    mc = list(mc)[0]
    ms = min(x.start for x in mr)
    me = max(x.end for x in mr)

    neg_strands = sum(1 for x in beds if x.strand == '-')
    pos_strands = len(beds) - neg_strands
    strand = '-' if neg_strands > pos_strands else '+'

    return mc, ms, me, strand


def prepare(args):
    """
    %prog prepare om_alignment.bed seq.fasta

    Given optical map alignment, prepare the patchers. Use --backbone to suggest
    which assembly is the major one, and the patchers will be extracted from
    another assembly.
    """
    from jcvi.formats.sizes import Sizes

    p = OptionParser(prepare.__doc__)
    p.add_option("--backbone", default="Scaffold",
                 help="Prefix of the backbone assembly [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    bedfile, fastafile = args
    bb = opts.backbone
    pf = bedfile.split(".")[0]

    # Make a uniq bed keeping backbone at redundant intervals
    bed = Bed(bedfile)
    uniqbed = Bed()
    key = lambda x: (x.seqid, x.start, x.end)
    is_bb = lambda x: x.accn.startswith(bb)
    for k, sb in groupby(bed, key=key):
        sb = list(sb)
        backbone = [x for x in sb if is_bb(x)]
        others = [x for x in sb if not is_bb(x)]
        if backbone and others:
            uniqbed.extend(backbone)
        else:
            uniqbed.extend(sb)

    uniqbedfile = bedfile.rsplit(".", 1)[0] + ".uniq.bed"
    uniqbed.print_to_file(uniqbedfile)

    # Condense adjacent intervals, allow some chaining
    bed = uniqbed
    key = lambda x: (is_bb, range_parse(x.accn).seqid)
    Flank = 10000
    sizes = Sizes(fastafile).mapping

    bed_fn = pf + ".patchers.bed"
    tpf_fn = pf + ".tpf"
    bed_fw = open(bed_fn, "w")
    tpf_fw = open(tpf_fn, "w")
    print >> tpf_fw, "\t".join(("telomere", pf, "na"))

    for k, sb in groupby(bed, key=key):
        sb = list(sb)
        chr, start, end, strand = merge_ranges(sb)
        size = sizes[chr]
        start = max(start - Flank, 0)
        end = min(end + Flank, size)

        id = "{0}:{1}-{2}".format(chr, start, end)
        print >> bed_fw, "\t".join(str(x) for x in (chr, start, end))
        print >> tpf_fw, "\t".join((id, pf, strand))

    bed_fw.close()
    tpf_fw.close()

    fastafn = pf + ".patchers.fasta"
    cmd = "fastaFromBed -fi {0} -bed {1} -fo {2}".\
            format(fastafile, bed_fn, fastafn)
    sh(cmd)


if __name__ == '__main__':
    main()
