#!/bin/bash
# Szoftveres NVMe tesztlemez (nvme-loop) hibás blokkokkal - a radtest kipróbálásához.
#   tests/loopdisk.sh up [méret_MB]   létrehozza és csatlakoztatja (S/N: RADTESTLOOP01)
#   tests/loopdisk.sh down            lebontja
# Utána: ./radtest.py run nvmeX --transport loop   vagy   ./radtest.py watch --transport loop
set -e
IMG=/var/tmp/radtest-loop.img
N=/sys/kernel/config/nvmet
case "$1" in
up)
	SIZE=${2:-1024}
	truncate -s ${SIZE}M $IMG
	LOOP=$(losetup -f --show $IMG)
	SEC=$(blockdev --getsz $LOOP)
	# két hibás tartomány: 1000-1003 és 500000-500063 (512 B szektor)
	dmsetup create radtest_loop <<EOT
0 1000 linear $LOOP 0
1000 4 error
1004 498996 linear $LOOP 1004
500000 64 error
500064 $((SEC - 500064)) linear $LOOP 500064
EOT
	modprobe nvmet nvme-loop
	mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
	mkdir $N/subsystems/radtestloop
	echo 1 > $N/subsystems/radtestloop/attr_allow_any_host
	echo -n RADTESTLOOP01 > $N/subsystems/radtestloop/attr_serial
	echo -n "RadTest Loop Disk" > $N/subsystems/radtestloop/attr_model
	mkdir $N/subsystems/radtestloop/namespaces/1
	echo -n /dev/mapper/radtest_loop > $N/subsystems/radtestloop/namespaces/1/device_path
	echo 1 > $N/subsystems/radtestloop/namespaces/1/enable
	mkdir -p $N/ports/9
	echo loop > $N/ports/9/addr_trtype
	ln -s $N/subsystems/radtestloop $N/ports/9/subsystems/radtestloop
	nvme connect -t loop -n radtestloop
	;;
down)
	nvme disconnect -n radtestloop || true
	rm -f $N/ports/9/subsystems/radtestloop
	rmdir $N/ports/9 2>/dev/null || true
	echo 0 > $N/subsystems/radtestloop/namespaces/1/enable 2>/dev/null || true
	rmdir $N/subsystems/radtestloop/namespaces/1 $N/subsystems/radtestloop 2>/dev/null || true
	dmsetup remove radtest_loop 2>/dev/null || true
	for l in $(losetup -j $IMG | cut -d: -f1); do losetup -d $l; done
	rm -f $IMG
	;;
*)
	echo "használat: $0 up [méret_MB] | down"; exit 2 ;;
esac
