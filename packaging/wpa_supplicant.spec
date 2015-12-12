Name:           wpa_supplicant
Version:        2.4
Release:        6
License:        BSD-3-Clause and GPL-2.0+
Summary:        WPA supplicant implementation
Url:            http://hostap.epitest.fi/wpa_supplicant/
Group:          Network & Connectivity/Wireless
Source0:    %{name}-%{version}.tar.gz
Source1001:     wpa_supplicant.manifest

BuildRequires: pkgconfig(openssl)
BuildRequires: pkgconfig(libssl)
BuildRequires: pkgconfig(libcrypto)
BuildRequires: pkgconfig(dbus-1)
BuildRequires: pkgconfig(libnl-2.0)
Requires(post): /sbin/ldconfig
Requires(postun): /sbin/ldconfig

%description
wpa_supplicant is an implementation of the WPA Supplicant component,
i.e., the part that runs in the client stations. It implements key
negotiation with a WPA Authenticator and it controls the roaming and
IEEE 802.11 authentication/association of the wlan driver.

%prep
%setup -q

%build

%if "%{?profile}" == "mobile"
%if "%{?tizen_target_name}" == "Z130H"
CONFIG_TIZEN_MOBILE=y; export CONFIG_TIZEN_MOBILE
%else
%if "%{?tizen_target_name}" == "TM1"
CONFIG_TIZEN_WLAN_BOARD_SPRD=y; export CONFIG_TIZEN_WLAN_BOARD_SPRD
%else
CONFIG_BCM_DRIVER_V115=y; export CONFIG_BCM_DRIVER_V115
%endif
%endif
%else
%if "%{?profile}" == "tv"
CONFIG_TIZEN_TV_BOARD_PRD=y; export CONFIG_TIZEN_TV_BOARD_PRD
%endif
%endif

cp %{SOURCE1001} .
cp -v configurations/tizen.config wpa_supplicant/.config
cp -v configurations/tizen_hostapd.config hostapd/.config
make %{?_smp_mflags} -C wpa_supplicant all
make -C hostapd clean
make %{?_smp_mflags} -C hostapd all

%install
mkdir -p %{buildroot}%{_sbindir}/systemd/
#mkdir -p %{buildroot}%{_sbindir}/dbus/

cp -v wpa_supplicant/wpa_supplicant %{buildroot}%{_sbindir}/
cp -v wpa_supplicant/wpa_cli %{buildroot}%{_sbindir}/
cp -v hostapd/hostapd %{buildroot}%{_sbindir}/
cp -v hostapd/hostapd_cli %{buildroot}%{_sbindir}/
cp -v files/wpa_supp.sh %{buildroot}%{_sbindir}/

# Configurations
mkdir -p %{buildroot}%{_sysconfdir}/wpa_supplicant/
cp -v wpa_supplicant/wpa_supplicant.conf %{buildroot}%{_sysconfdir}/wpa_supplicant/wpa_supplicant.conf
cp -v hostapd/hostapd.conf %{buildroot}%{_sysconfdir}/wpa_supplicant/hostapd.conf

# D-Bus
mkdir -p %{buildroot}%{_sysconfdir}/dbus-1/system.d/
cp wpa_supplicant/dbus/dbus-wpa_supplicant.conf %{buildroot}%{_sysconfdir}/dbus-1/system.d/wpa_supplicant.conf
#mkdir -p %{buildroot}%{_datadir}/dbus-1/services/
#mkdir -p %{buildroot}%{_datadir}/dbus-1/system-services/
#cp wpa_supplicant/dbus/fi.epitest.hostap.WPASupplicant.service %{buildroot}%{_datadir}/dbus-1/services/
#cp wpa_supplicant/dbus/fi.w1.wpa_supplicant1.service %{buildroot}%{_datadir}/dbus-1/system-services/


# sanitise the example configuration
mkdir -p %{buildroot}%{_defaultdocdir}/wpasupplicant
sed 's/^\([^#]\+=.*\|}\)/#\1/' < ./wpa_supplicant/wpa_supplicant.conf | gzip > %{buildroot}%{_defaultdocdir}/wpasupplicant/README.wpa_supplicant.conf.gz

# install systemd service file
#mkdir -p %{buildroot}%{_libdir}/systemd/system
#install -m 0644 %{SOURCE1} %{buildroot}%{_libdir}/systemd/system/
#mkdir -p %{buildroot}%{_libdir}/systemd/system/network.target.wants
#ln -s ../wpa_supplicant.service %{buildroot}%{_libdir}/systemd/system/network.target.wants/wpa_supplicant.service

rm -rf %{buildroot}%{_sbindir}/systemd/
#rm -rf %{buildroot}%{_sbindir}/dbus/
rm -rf %{buildroot}%{_sbindir}/wpa_passphrase

%post -p /sbin/ldconfig

%postun -p /sbin/ldconfig


%files
%manifest wpa_supplicant.manifest
%{_sbindir}/wpa_cli
%{_sbindir}/wpa_supplicant
%{_sbindir}/hostapd
%{_sbindir}/hostapd_cli
%attr(500,root,root) %{_sbindir}/wpa_supp.sh
%attr(644,-,-) %{_sysconfdir}/dbus-1/system.d/*.conf
#%attr(644,-,-) %{_datadir}/dbus-1/services/*.service
#%attr(644,-,-) %{_datadir}/dbus-1/system-services/*.service
%attr(644,-,-) %{_sysconfdir}/wpa_supplicant/*.conf
%{_defaultdocdir}/wpasupplicant/README.wpa_supplicant.*
#%{_libdir}/systemd/system/wpa_supplicant.service
#%{_libdir}/systemd/system/network.target.wants/wpa_supplicant.service
