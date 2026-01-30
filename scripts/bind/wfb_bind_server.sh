#!/bin/bash
set -e
IFS=$'\t'

report_err()
{
    echo $'ERR\tInternal error'
}

show_version()
{
    set -e
    trap report_err ERR

    echo $'OK\t'"$(wfb-server --version | head -n1)"
}

# Update only [common] section in existing config file
update_common_section()
{
    local new_cfg="$1"
    local existing_cfg="/etc/wifibroadcast.cfg"
    local tmp_cfg="${existing_cfg}.tmp"
    local common_section="${tmp_cfg}.common"

    awk '
        BEGIN { in_common = 0 }
        /^\[common\]/ { 
            in_common = 1
            print
            next
        }
        /^\[[a-zA-Z_][a-zA-Z0-9_]*\]/ && in_common { 
            exit
        }
        in_common { 
            print
        }
    ' "$new_cfg" > "$common_section"

    # Remove old [common] section from existing config
    awk '
        BEGIN { in_common = 0; found_common = 0 }
        /^\[common\]/ { 
            in_common = 1
            found_common = 1
            next
        }
        /^\[[a-zA-Z_][a-zA-Z0-9_]*\]/ && in_common { 
            in_common = 0
            print
            next
        }
        !in_common { 
            print
        }
        in_common { 
            # Skip old common section content
            next
        }
    ' "$existing_cfg" > "$tmp_cfg"

    if [ -s "$common_section" ] && [ "$(tail -c 1 "$common_section")" != "" ]; then
        echo "" >> "$common_section"
    fi
    
    # Check if common_section is empty (should not happen)
    if [ ! -s "$common_section" ]; then
        echo "Warning: common section is empty, keeping existing config" >&2
        rm -f "$tmp_cfg" "$common_section"
        return
    fi
    
    # Check if tmp_cfg is empty (should not happen)
    if [ ! -s "$tmp_cfg" ]; then
        echo "Warning: existing config is empty, using only new common section" >&2
        cp "$common_section" "$existing_cfg"
        rm -f "$tmp_cfg" "$common_section"
        return
    fi
    
    cat "$common_section" "$tmp_cfg" > "$existing_cfg"

    rm -f "$tmp_cfg" "$common_section"
}

do_bind()
{
    set -e
    trap report_err ERR

    tmpdir=$(mktemp -d)
    echo "$1" | base64 -d | tar xz -C "$tmpdir"

    cd "$tmpdir"

    if ! [ -f checksum.txt ] || ! sha1sum --quiet --status --strict -c checksum.txt
    then
        echo $'ERR\tChecksum failed'
        exit 0
    fi

    if [ -f wifibroadcast.cfg ]
    then
        if [ ! -f /etc/wifibroadcast.cfg ]
        then
            cp wifibroadcast.cfg /etc/
        else
            update_common_section wifibroadcast.cfg
        fi
    fi

    # Copy other files normally
    for i in drone.key bind.yaml
    do
        if [ -f $i ]
        then
            cp $i /etc/
        fi
    done

    rm -r "$tmpdir"
    echo "OK"
}

do_unbind()
{
    set -e
    trap report_err ERR

    rm -f /etc/drone.key
    echo "OK"
}

while read -r cmd arg
do
    case $cmd in
        "VERSION")
            show_version
            ;;
        "BIND")
            do_bind "$arg"
            ;;
        "UNBIND")
            do_unbind
            ;;
        *)
            echo $'ERR\tUnsupported command'
            ;;
    esac
done
