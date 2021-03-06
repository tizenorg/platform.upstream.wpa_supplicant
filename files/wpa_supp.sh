#!/bin/sh
PATH=/bin:/usr/bin:/sbin:/usr/sbin

start()
{
	HARDWARE_MODEL=`/bin/grep Hardware /proc/cpuinfo | /bin/awk "{print \\$3}"`

	case $HARDWARE_MODEL in
		"U1SLP" | "U1HD") /bin/echo "This is U1SLP"
			/usr/sbin/wpa_supplicant -u -t -B -d -Dwext -f/opt/usr/data/network/wpa_supplicant.log
		;;
		"SLP7_C210")	 /bin/echo "This is C210"
			/usr/sbin/wpa_supplicant -u -t -B -d -Dwext -f/opt/usr/data/network/wpa_supplicant.log
		;;
		"SLP10_C210")	/bin/echo "This is C210"
			/usr/sbin/wpa_supplicant -u -t -B -d -Dwext -f/opt/usr/data/network/wpa_supplicant.log
		;;

		"Samsung")/bin/echo "This is Tizen TV"
			/usr/sbin/wpa_supplicant -u -t -B -dd -O /var/run/wpa_supplicant -g/var/run/wpa_global -f/var/log/wpa_supplicant.log
		;;
		*)
			/usr/sbin/wpa_supplicant -u -t -B -dd -O /var/run/wpa_supplicant -g/var/run/wpa_global -f/var/log/wpa_supplicant.log
		;;
	esac
}

stop()
{
	/usr/bin/pkill -x wpa_supplicant
	/bin/usleep 150000
}

case $1 in
"start")
start
;;
"stop")
stop
;;
*)
/bin/echo wpa_supp.sh [start] [stop]
exit 1
;;
esac
