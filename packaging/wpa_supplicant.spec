Name:           wpa_supplicant
Version:        1.0
Release:        0
License:        BSD-3-Clause ; GPL-2.0+
Summary:        WPA supplicant implementation
Url:            http://hostap.epitest.fi/wpa_supplicant/
Group:          Productivity/Networking/Other
Source:         http://hostap.epitest.fi/releases/wpa_supplicant-%{version}.tar.bz2
Source1:        config
Source2:        wpa_supplicant.conf
Source3:        fi.epitest.hostap.WPASupplicant.service
Source4:        fi.w1.wpa_supplicant1.service
Source5:        wpa_supplicant.service
BuildRequires:  dbus-devel
BuildRequires:  libnl-devel
BuildRequires:  openssl-devel
BuildRequires:  pkg-config
BuildRequires:  readline-devel

%description
wpa_supplicant is an implementation of the WPA Supplicant component,
i.e., the part that runs in the client stations. It implements key
negotiation with a WPA Authenticator and it controls the roaming and
IEEE 802.11 authentication/association of the wlan driver.

%prep
%setup -q -n wpa_supplicant-%{version}
rm -rf wpa_supplicant-%{version}/patches
cp %{SOURCE1} wpa_supplicant/.config

%build
cd wpa_supplicant
CFLAGS="%{optflags}" make V=1 %{?_smp_mflags}

%install
install -d %{buildroot}/%{_sbindir}
install -m 0755 wpa_supplicant/wpa_cli %{buildroot}%{_sbindir}
install -m 0755 wpa_supplicant/wpa_passphrase %{buildroot}%{_sbindir}
install -m 0755 wpa_supplicant/wpa_supplicant %{buildroot}%{_sbindir}
install -d %{buildroot}%{_sysconfdir}/dbus-1/system.d
install -m 0644 wpa_supplicant/dbus/dbus-wpa_supplicant.conf %{buildroot}%{_sysconfdir}/dbus-1/system.d/wpa_supplicant.conf
install -d %{buildroot}/%{_sysconfdir}/%{name}
install -m 0600 %{SOURCE2} %{buildroot}/%{_sysconfdir}/%{name}
install -d %{buildroot}/%{_datadir}/dbus-1/system-services
install -m 0644 %{SOURCE3} %{buildroot}/%{_datadir}/dbus-1/system-services
install -m 0644 %{SOURCE4} %{buildroot}/%{_datadir}/dbus-1/system-services
install -d %{buildroot}/%{_localstatedir}/run/%{name}
install -d %{buildroot}%{_mandir}/man{5,8}
install -m 0644 wpa_supplicant/doc/docbook/*.8 %{buildroot}%{_mandir}/man8
install -m 0644 wpa_supplicant/doc/docbook/*.5 %{buildroot}%{_mandir}/man5

# install systemd service file
mkdir -p %{buildroot}%{_unitdir}
install -m 0644 %{SOURCE5} %{buildroot}%{_unitdir}
mkdir -p %{buildroot}%{_unitdir}/network.target.wants
ln -s ../wpa_supplicant.service %{buildroot}%{_unitdir}/network.target.wants/wpa_supplicant.service

%docs_package

%files
%defattr(-,root,root)
%{_sbindir}/wpa_cli
%{_sbindir}/wpa_passphrase
%{_sbindir}/wpa_supplicant
%config %{_sysconfdir}/dbus-1/system.d/%{name}.conf
%{_datadir}/dbus-1/system-services
%config %{_sysconfdir}/%{name}/%{name}.conf
%dir %{_localstatedir}/run/%{name}
%ghost %{_localstatedir}/run/%{name}
%dir %{_sysconfdir}/%{name}
%{_unitdir}/wpa_supplicant.service
%{_unitdir}/network.target.wants/wpa_supplicant.service

%changelog
