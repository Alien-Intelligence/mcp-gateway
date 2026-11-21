#!/bin/sh
# Scan the FULL history of the repository (every commit of every ref: messages, author and committer
# identities, the content of every change, the ref names and tag messages) against a deny-list of
# regular expressions that must never appear in a public mirror: customer names, internal host
# names, personal names. Run by the CI job `history-denylist`; also runs on a laptop.
#
# The deny-list is itself sensitive, so it is NOT in the repository. It is read from the file named
# by HISTORY_DENYLIST_FILE (a File-type CI variable): one Perl regular expression per line, blank
# lines and lines starting with # ignored. Case sensitivity is the pattern's own: write (?i) to
# ignore case. The script fails, never passes silently, when the variable is missing, the file is
# unreadable, it holds no pattern, or a pattern is not a valid Perl regular expression.
#
# On a hit it prints the commit, where it hit (header: identities and message, ref, diff) and the pattern, never
# the matched text, and exits 1. Exit status: 0 clean, 1 at least one hit, 2 the scan could not run.
#
# Needs git and perl.
set -eu

if [ -z "${HISTORY_DENYLIST_FILE:-}" ]; then
    echo "ERROR: HISTORY_DENYLIST_FILE is not set: the history cannot be checked, and a check that is skipped is not a pass." >&2
    exit 2
fi
if [ ! -r "$HISTORY_DENYLIST_FILE" ]; then
    echo "ERROR: HISTORY_DENYLIST_FILE does not name a readable file." >&2
    exit 2
fi
for tool in git perl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: $tool is not installed." >&2
        exit 2
    fi
done

work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT INT TERM

# The history as one stream, written to a file so that a failing git stops the scan (a pipe would hide
# it). Per commit: a marker line, the author and committer identities, the message, then the diff.
# -m: a merge commit's own changes are shown against each parent, not only its conflict resolution.
# Then every ref name, the tagger identity of annotated tags, and the tag messages.
history="$work_dir/history.txt"
{
    git log --all --root -m -p --format='%x01COMMIT %H%n%an <%ae>%n%cn <%ce>%n%B%x01DIFF' --
    git for-each-ref --format='%01REF %(refname)%n%(taggername) %(taggeremail)%01DIFF'
    git for-each-ref refs/tags --format='%01REF %(refname)%n%(contents)%01DIFF'
} >"$history" || {
    echo "ERROR: git could not read the history." >&2
    exit 2
}

# Every pattern is compiled before the history is read, so a typo cannot silently weaken the scan.
perl -e '
    use strict;
    use warnings;
    my ($denylist, $history) = @ARGV;
    open(my $fh, "<", $denylist) or do { print STDERR "ERROR: cannot read the deny-list\n"; exit 2 };
    my @patterns;
    while (my $line = <$fh>) {
        chomp $line;
        $line =~ s/\r$//;
        next if $line =~ /^\s*$/ || $line =~ /^\s*#/;
        my $re = eval { qr/$line/ };
        if (!defined $re) {
            print STDERR "ERROR: deny-list line ", $., " is not a valid Perl regular expression\n";
            exit 2;
        }
        push @patterns, [$line, $re];
    }
    close $fh;
    if (!@patterns) {
        print STDERR "ERROR: the deny-list holds no pattern: an empty list checks nothing\n";
        exit 2;
    }
    open(my $in, "<", $history) or do { print STDERR "ERROR: cannot read the history\n"; exit 2 };
    my ($commit, $region, %seen, $hits, $lines) = ("(none)", "header", (), 0, 0);
    while (my $line = <$in>) {
        $lines++;
        chomp $line;
        if ($line =~ /^\x01(COMMIT|REF) (\S+)/) {
            $commit = $1 eq "COMMIT" ? substr($2, 0, 12) : "ref $2";
            $region = "header";
        } elsif ($line =~ /^\x01DIFF/) {
            $region = "diff";
            next;
        }
        for my $i (0 .. $#patterns) {
            next unless $line =~ $patterns[$i][1];
            next if $seen{"$commit\0$i\0$region"}++;
            $hits++;
            print "HIT commit $commit ($region) pattern: $patterns[$i][0]\n";
        }
    }
    close $in;
    if ($lines == 0) {
        print STDERR "ERROR: git produced no history to scan\n";
        exit 2;
    }
    printf "scanned %d lines of history against %d patterns: %s\n", $lines, scalar(@patterns), $hits ? "$hits hit(s)" : "no hit";
    exit($hits ? 1 : 0);
' "$HISTORY_DENYLIST_FILE" "$history"
