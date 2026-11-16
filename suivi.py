#!/usr/bin/env python3
"""Collecte TER Villefranche-sur-Saône <-> Lyon-Part-Dieu.
Données SNCF (ODbL), via Train Nomade. Python standard uniquement.
"""
import csv
import io
import json
import os
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
TZ = ZoneInfo('Europe/Paris')
NOW = datetime.now(TZ)
V = '87721332'
L = '87723197'
API = 'https://api.trainnomade.fr'
COLUMNS = ['date','sens','numero','depart_prevu','arrivee_prevue','depart_reel_ou_estime','arrivee_reelle_ou_estimee','retard_arrivee_min','statut','preuve_arrivee','derniere_observation','source']
SOURCE = 'Données SNCF (ODbL), via Train Nomade'

def fetch(path):
    req = urllib.request.Request(API+path, headers={'User-Agent': 'SuiviTERVillefranchePartDieu/1.0 (usage personnel)', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=40) as f:
        return json.load(f)

def minutes(m):
    if m is None: return ''
    m = int(m)
    return f'{m//60:02d}:{m%60:02d}'

def seconds(s):
    if s is None: return ''
    s = int(s)
    return f'{s//3600:02d}:{(s%3600)//60:02d}'

def eligible(day, start, mask):
    offset = (day-date.fromisoformat(start)).days
    return 0 <= offset <= 13 and bool(int(mask) & (1 << offset))

def schedule(slug, target_date):
    h = fetch('/gares/horaires/'+slug+'.json')
    result = {}
    for n, prod, origin, dest, arr, dep, mask in h.get('trains',[]):
        if not eligible(target_date, h['du'], mask): continue
        if 'TER' not in str(h['produits'][int(prod)]).upper(): continue
        # Les numéros commerciaux sont uniques en général par jour ; conserver une liste en cas de doublons
        result.setdefault(str(n), []).append((arr,dep))
    return result

def load_rows():
    p=DATA/'trains.csv'
    if not p.exists(): return {}
    with p.open(newline='',encoding='utf-8') as f:
        return {(r['date'],r['sens'],r['numero'],r['depart_prevu']):r for r in csv.DictReader(f)}

def safe_write(path, rows, cols):
    temp=path.with_suffix('.tmp')
    with temp.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=cols)
        w.writeheader(); w.writerows(rows)
    os.replace(temp,path)

def main():
    day=NOW.date()
    # Horaires théoriques calculés chaque nuit ; ne pas considérer une absence temps réel comme suppression
    planned = {}
    try:
        v=schedule('gare-de-villefranche-sur-saone',day)
        l=schedule('gare-lyon-part-dieu',day)
        for n in v.keys() & l.keys():
            for v_arr,v_dep in v[n]:
                for l_arr,l_dep in l[n]:
                    if v_dep is not None and l_arr is not None and v_dep < l_arr:
                        planned[(n,'Villefranche → Part-Dieu',minutes(v_dep))] = (v_dep*60,l_arr*60)
                    if l_dep is not None and v_arr is not None and l_dep < v_arr:
                        planned[(n,'Part-Dieu → Villefranche',minutes(l_dep))] = (l_dep*60,v_arr*60)
    except Exception as e:
        print('AVERTISSEMENT: horaires théoriques indisponibles:',e)
    live = fetch('/sncf.json')
    if live.get('jour') != day.isoformat():
        raise RuntimeError('Flux temps réel portant sur un autre jour : refus de mise à jour')
    if live.get('perime_apres') and datetime.fromisoformat(live['perime_apres']) < NOW:
        raise RuntimeError('Flux temps réel périmé : refus de mise à jour')
    rows=load_rows()
    # Enregistrer le programme à l'avance, même si la course n'est pas encore dans le flux
    for (n,sens,dep_txt),(dep_s,arr_s) in planned.items():
        key=(day.isoformat(),sens,n,dep_txt)
        if key not in rows:
            rows[key]=dict(zip(COLUMNS,[day.isoformat(),sens,n,dep_txt,seconds(arr_s),'','','','prévu','non',NOW.isoformat(),SOURCE]))
    for n,t in live.get('trains',{}).items():
        if t.get('p') not in ('regionalRail','interregionalRail',None): continue
        stops=t.get('ar',[])
        vv=[(i,s) for i,s in enumerate(stops) if str(s.get('s'))==V]
        ll=[(i,s) for i,s in enumerate(stops) if str(s.get('s'))==L]
        for vi,vs in vv:
            for li,ls in ll:
                if vi==li: continue
                if vi<li:
                    sens='Villefranche → Part-Dieu'; depart=vs; arrivee=ls
                else:
                    sens='Part-Dieu → Villefranche'; depart=ls; arrivee=vs
                if depart.get('d') is None or arrivee.get('a') is None: continue
                dep_s=int(depart['d']); arr_s=int(arrivee['a'])
                n_base=str(n).split('/')[0]
                key=(day.isoformat(),sens,n_base,seconds(dep_s))
                # Corriger approximation d'horaires à partir du flux temps réel si nécessaire
                if key not in rows:
                    match=[k for k in rows if k[0]==day.isoformat() and k[1]==sens and k[2]==n_base]
                    if len(match)==1: key=match[0]
                old=rows.get(key,{})
                cancelled=bool(t.get('an') or depart.get('x') or arrivee.get('x'))
                confirmed=bool(arrivee.get('p'))
                delay=int(arrivee.get('ra',0))
                status='supprimé' if cancelled else 'arrivé (passage constaté)' if confirmed else 'estimé / en circulation'
                # Ne jamais remplacer une arrivée réellement constatée par une estimation ultérieure
                if old.get('preuve_arrivee')=='oui' and not cancelled: continue
                dep_real=seconds(dep_s+int(depart.get('rd',0)))
                arr_real=seconds(arr_s+delay)
                rows[key]={'date':day.isoformat(),'sens':sens,'numero':n_base,'depart_prevu':key[3],
                           'arrivee_prevue':seconds(arr_s),'depart_reel_ou_estime':dep_real,
                           'arrivee_reelle_ou_estimee':arr_real,'retard_arrivee_min':f'{delay/60:.1f}' if not cancelled else '',
                           'statut':status,'preuve_arrivee':'oui' if confirmed else 'non',
                           'derniere_observation':NOW.isoformat(),'source':SOURCE}
    safe_write(DATA/'trains.csv',[rows[k] for k in sorted(rows)],COLUMNS)
    # Le bilan ne traite que les arrivées constatées; les suppressions explicitement déclarées sont séparées
    stats={}
    for r in rows.values():
        key=(r['date'],r['sens']); item=stats.setdefault(key,{'date':key[0],'sens':key[1], 'trains_prevus_observes':0,'arrivees_constatees':0,'retards_5min':0,'retards_15min':0,'supprimes_declares':0,'sans_resultat_confirme':0,'retard_moyen_min':'','ponctualite_arrivees_pct':'' ,'_delays':[]})
        item['trains_prevus_observes']+=1
        if r['statut']=='supprimé': item['supprimes_declares']+=1
        elif r['preuve_arrivee']=='oui':
            d=float(r['retard_arrivee_min']); item['_delays'].append(d); item['arrivees_constatees']+=1
            if d>=5: item['retards_5min']+=1
            if d>=15: item['retards_15min']+=1
        else: item['sans_resultat_confirme']+=1
    for x in stats.values():
        ds=x.pop('_delays')
        if ds:
            x['retard_moyen_min']=f'{sum(ds)/len(ds):.1f}'
            x['ponctualite_arrivees_pct']=f'{100*(len(ds)-x["retards_5min"])/len(ds):.1f}'
    cols=['date','sens','trains_prevus_observes','arrivees_constatees','retards_5min','retards_15min','supprimes_declares','sans_resultat_confirme','retard_moyen_min','ponctualite_arrivees_pct']
    safe_write(DATA/'bilan_quotidien.csv',[stats[k] for k in sorted(stats)],cols)
    print('Collecte terminée:',len(rows),'trains; journées/sens:',len(stats))

if __name__=='__main__': main()
