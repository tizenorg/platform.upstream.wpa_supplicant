Name:           wpa_supplicant
Version:        2.0+1171+g50acc38
Release:        0
License:        BSD-3-Clause and GPL-2.0+
Summary:        WPA supplicant implementation
Url:            http://hostap.epitest.fi/wpa_supplicant/
Group:          Connectivity/Wireless
Source:         http://hostap.epitest.fi/releases/wpa_supplicant-%{version}.tar.gz
Source1:        config
Source1001: 	wpa_supplicant.manifest
BuildRequires:  dbus-devel
BuildRequires:  libnl-devel
BuildRequires:  openssl-devel
BuildRequires:  pkg-config
BuildRequires:  readline-devel
BuildRequires:  systemd

%description
wpa_supplicant is an implementation of the WPA Supplicant component,
i.e., the part that runs in the client stations. It implements key
negotiation with a WPA Authenticator and it controls the roaming and
IEEE 802.11 authentication/association of the wlan driver.

%prep
%setup -q -n wpa_supplicant-%{version}
cp %{SOURCE1001} .
rm -rf wpa_supplicant-%{version}/patches
cp %{SOURCE1} wpa_supplicant/.config

%build
cd wpa_supplicant
CFLAGS="%{optflags}" make V=1 BINDIR=%{_sbindir} %{?_smp_mflags}


%install
install -d %{buildroot}/%{_sbindir}
install -m 0755 wpa_supplicant/wpa_cli %{buildroot}%{_sbindir}
install -m 0755 wpa_supplicant/wpa_passphrase %{buildroot}%{_sbindir}
install -m 0755 wpa_supplicant/wpa_supplicant %{buildroot}%{_sbindir}
install -d %{buildroot}%{_sysconfdir}/dbus-1/system.d
install -m 0644 wpa_supplicant/dbus/dbus-wpa_supplicant.conf %{buildroot}%{_sysconfdir}/dbus-1/system.d/wpa_supplicant.conf
install -d %{buildroot}/%{_sysconfdir}/%{name}
install -d %{buildroot}/%{_datadir}/dbus-1/system-services
install -m 0644 wpa_supplicant/dbus/fi.epitest.hostap.WPASupplicant.service %{buildroot}/%{_datadir}/dbus-1/system-services
install -m 0644 wpa_supplicant/dbus/fi.w1.wpa_supplicant1.service %{buildroot}/%{_datadir}/dbus-1/system-services
install -d %{buildroot}/%{_localstatedir}/run/%{name}
install -d %{buildroot}%{_mandir}/man{5,8}
install -m 0644 wpa_supplicant/doc/docbook/*.8 %{buildroot}%{_mandir}/man8 || :
install -m 0644 wpa_supplicant/doc/docbook/*.5 %{buildroot}%{_mandir}/man5 || :

# install systemd service file
mkdir -p %{buildroot}%{_unitdir}
install -m 0644 wpa_supplicant/systemd/wpa_supplicant.service %{buildroot}%{_unitdir}
mkdir -p %{buildroot}%{_unitdir}/network.target.wants
ln -s ../wpa_supplicant.service %{buildroot}%{_unitdir}/network.target.wants/wpa_supplicant.service

%docs_package


%files
%manifest %{name}.manifest
%defattr(-,root,root)
%license COPYING
%{_sbindir}/wpa_cli
%{_sbindir}/wpa_passphrase
%{_sbindir}/wpa_supplicant
%config %{_sysconfdir}/dbus-1/system.d/%{name}.conf
%{_datadir}/dbus-1/system-services
%dir %{_localstatedir}/run/%{name}
%ghost /var/run/%{name}
%{_unitdir}/wpa_supplicant.service
%{_unitdir}/network.target.wants/wpa_supplicant.service
