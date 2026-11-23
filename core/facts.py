"""
Track Facts & GenX Trivia Provider for GenX71 AutoVJ.
Provides 3 concise, engaging facts per track via:
  1. SQLite cached facts (tracks.facts_json)
  2. Curated GenX trivia database for the built-in 1978-1996 chart catalog
  3. Live Wikipedia API lookup for custom/newly added tracks
  4. Clean chart-metadata fallback so every clip always has 3 facts
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import requests

from core.db import update_track_facts

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "GenX71AutoVJ/2.0 (https://github.com/sdewis/genx71_autovj)"}

# Curated 3 concise facts per track for the built-in GenX catalog (1978-1996).
# Keyed by (artist.lower(), title.lower()).
CURATED_TRACK_FACTS: Dict[Tuple[str, str], List[str]] = {
    ("kate bush", "wuthering heights"): [
        "Written by Kate Bush at age 18 after watching a BBC adaptation of the novel.",
        "First UK #1 hit ever written and performed by a female artist.",
        "Kate Bush shares a July 30 birthday with Wuthering Heights author Emily Brontë.",
    ],
    ("blondie", "heart of glass"): [
        "Started in 1974 as a reggae-style demo called 'Once I Had a Love'.",
        "Producer Mike Chapman used a Roland CR-78 drum machine for its disco pulse.",
        "Hit #1 in both the UK and US, selling over a million copies in each.",
    ],
    ("the buggles", "video killed the radio star"): [
        "First music video ever aired on MTV when the channel launched on Aug 1, 1981.",
        "Hans Zimmer briefly appears playing keyboards in the iconic music video.",
        "Co-written by Trevor Horn and Geoff Downes, who later joined prog band Yes.",
    ],
    ("gary numan", "cars"): [
        "Inspired by a real road-rage incident Gary Numan experienced in London traffic.",
        "Built around a Polymoog synthesizer riff with zero guitars in the mix.",
        "Hit #1 in the UK in 1979 and reached #9 on the US Billboard Hot 100.",
    ],
    ("the clash", "london calling"): [
        "The title borrows from the WWII BBC World Service radio station ID.",
        "Its iconic cover art parodies Elvis Presley's 1956 debut album sleeve.",
        "Recorded at Wessex Sound Studios in London with producer Guy Stevens.",
    ],
    ("joy division", "love will tear us apart"): [
        "Released in June 1980, one month after frontman Ian Curtis passed away.",
        "The song's title is inscribed on Ian Curtis's memorial stone in Macclesfield.",
        "Became Joy Division's highest-charting UK single, peaking at #13.",
    ],
    ("the jam", "going underground"): [
        "Became The Jam's very first UK #1 single, debuting straight at the top spot.",
        "Originally planned as a double A-side with 'Dreams of Children'.",
        "Paul Weller wrote it as a sharp protest against political apathy in 1980.",
    ],
    ("soft cell", "tainted love"): [
        "A synth-pop cover of Gloria Jones's 1965 Northern Soul classic.",
        "Spent a then-record 43 consecutive weeks on the US Billboard Hot 100.",
        "The famous 12-inch version segues seamlessly into The Supremes' 'Where Did Our Love Go'.",
    ],
    ("the human league", "don't you want me"): [
        "Philip Oakey initially hated the track and tried to bury it at the end of the album.",
        "Became the UK Christmas #1 of 1981 and sold over 1.5 million copies in Britain.",
        "Features backing vocals by Susanne Sulley and Joanne Catherall, recruited from a Sheffield club.",
    ],
    ("orchestral manoeuvres in the dark", "enola gay"): [
        "Named after the B-29 bomber that dropped the atomic bomb on Hiroshima.",
        "An anti-war synth anthem with no chorus—just a driving instrumental hook.",
        "Recorded in just one take on a Korg Micro-Preset synth in 1980.",
    ],
    ("depeche mode", "just can't get enough"): [
        "Written by founding member Vince Clarke before he left to form Yazoo and Erasure.",
        "Depeche Mode's first UK Top 10 hit, peaking at #8 in September 1981.",
        "Its music video is the only Depeche Mode clip to feature Vince Clarke.",
    ],
    ("ultravox", "vienna"): [
        "Famously kept off UK #1 for four weeks by Joe Dolce's 'Shaddap You Face'.",
        "Inspired by Carol Reed's classic 1949 film noir thriller 'The Third Man'.",
        "Billy Currie's dramatic viola solo became one of the defining sounds of 1981.",
    ],
    ("duran duran", "rio"): [
        "The laughing intro is actually the sound of metal rods thrown onto piano strings.",
        "Its yacht-filled video was filmed in Antigua and became an MTV staple.",
        "The iconic album sleeve was painted by pop artist Patrick Nagel.",
    ],
    ("a flock of seagulls", "i ran (so far away)"): [
        "One of the earliest British New Wave hits propelled to US Top 10 fame by MTV.",
        "Its mirrored-room video was shot on a tiny budget with aluminum foil walls.",
        "Mike Score's waterfall hairstyle became an instant symbol of 1982 pop culture.",
    ],
    ("dexys midnight runners", "come on eileen"): [
        "Hit #1 in both the UK and US, knocking Michael Jackson's 'Billie Jean' off #1 in America.",
        "Named the Best British Single at the 1983 Brit Awards.",
        "Its tempo accelerates gradually during the bridge before exploding back into the chorus.",
    ],
    ("tears for fears", "mad world"): [
        "Roland Orzabal wrote the song at age 19 on an acoustic guitar in Bath.",
        "Curt Smith sang lead vocals, turning it into the band's first UK Top 3 hit.",
        "Gary Jules's stripped-down cover later became the 2003 UK Christmas #1.",
    ],
    ("new order", "blue monday"): [
        "Remains the best-selling 12-inch vinyl single of all time in the UK.",
        "Its floppy-disk sleeve by Peter Saville cost so much that Factory Records lost money per copy.",
        "Built on an Oberheim DMX drum machine so the band could leave the stage for encores.",
    ],
    ("eurythmics", "sweet dreams (are made of this)"): [
        "Recorded in an attic above a picture-framing factory in North London on an 8-track.",
        "Dave Stewart found the signature synth bassline by accidentally playing a track backwards.",
        "Hit #1 on the US Billboard Hot 100 and #2 in the UK in 1983.",
    ],
    ("spandau ballet", "true"): [
        "Gary Kemp wrote it as a tribute to Marvin Gaye and Motown soul.",
        "Spent four weeks at UK #1 and was famously sampled on P.M. Dawn's 1991 #1 hit.",
        "Recorded at Compass Point Studios in Nassau, Bahamas.",
    ],
    ("david bowie", "let's dance"): [
        "Co-produced by Chic's Nile Rodgers and features a young Stevie Ray Vaughan on lead guitar.",
        "Bowie's only single to hit #1 in both the UK and the US simultaneously.",
        "Recorded in just 17 days at the Power Station studio in New York.",
    ],
    ("frankie goes to hollywood", "relax"): [
        "Banned by BBC Radio 1 in January 1984—which only rocketed it straight to UK #1.",
        "Produced by Trevor Horn, who re-recorded nearly all the instrumentation himself.",
        "Remains one of the top 10 best-selling UK singles of all time.",
    ],
    ("bronski beat", "smalltown boy"): [
        "A groundbreaking 1984 synth-pop anthem addressing homophobia and leaving home.",
        "Jimmy Somerville's soaring falsetto carried it to #3 on the UK Singles Chart.",
        "Its synth hook has been sampled in dozens of dance hits over four decades.",
    ],
    ("prince", "when doves cry"): [
        "Prince famously stripped the entire bassline out at the last minute before mastering.",
        "Spent five weeks at US #1 and became Billboard's #1 song of 1984.",
        "Prince played every single instrument on the track himself.",
    ],
    ("talk talk", "it's my life"): [
        "Peaked at #13 in the UK on reissue and became a massive European radio classic.",
        "No Doubt's 2003 cover introduced the song to a whole new generation.",
        "Mark Hollis wrote the lyrics about feeling trapped in repetitive routines.",
    ],
    ("alphaville", "forever young"): [
        "Recorded in West Berlin by German synth-pop trio Alphaville in 1984.",
        "Became a global GenX slow-dance anthem despite missing the UK Top 40.",
        "Jay-Z sampled the chorus for his 2009 hit 'Young Forever'.",
    ],
    ("a-ha", "take on me"): [
        "Its iconic pencil-sketch video took 16 weeks and 3,000 rotoscoped drawings to animate.",
        "Failed twice on the charts before a 1985 re-recording sent it to US #1.",
        "Morten Harket's final chorus note spans over two and a half octaves.",
    ],
    ("tears for fears", "everybody wants to rule the world"): [
        "Added to 'Songs from the Big Chair' at the very last minute—and became their biggest hit.",
        "Built on a rare 12/8 shuffle groove inspired by 1950s rock and roll.",
        "Won Best British Single at the 1986 Brit Awards and spent two weeks at US #1.",
    ],
    ("simple minds", "don't you (forget about me)"): [
        "Written for 'The Breakfast Club' after Billy Idol and Bryan Ferry turned it down.",
        "Simple Minds recorded the whole song in just three hours in North London.",
        "Became the band's only US Billboard Hot 100 #1 single.",
    ],
    ("the cure", "in between days"): [
        "Marked The Cure's pivot toward bright, jangly acoustic-driven pop in 1985.",
        "Its swirling sock-cam video was directed by longtime collaborator Tim Pope.",
        "Became the band's first single to crack the US Billboard Hot 100.",
    ],
    ("pet shop boys", "west end girls"): [
        "Inspired by T.S. Eliot's poem 'The Waste Land' and Grandmaster Flash's 'The Message'.",
        "Originally flopped in 1984 before producer Stephen Hague remade it into a global #1.",
        "Hit #1 in both the UK and US in 1986 and won Best British Single at the Brit Awards.",
    ],
    ("kate bush", "running up that hill"): [
        "Originally titled 'A Deal with God', changed at EMI's request to avoid radio bans.",
        "Built around a Fairlight CMI synthesizer—one of the earliest digital samplers.",
        "Returned to UK #1 in 2022, 37 years after release, thanks to 'Stranger Things'.",
    ],
    ("the smiths", "there is a light that never goes out"): [
        "Never released as a UK single during The Smiths' lifetime—only issued in 1992.",
        "Johnny Marr composed the lush string arrangement on a synthesizer in the studio.",
        "Consistently voted one of the greatest British indie anthems of all time.",
    ],
    ("berlin", "take my breath away"): [
        "Written by Giorgio Moroder and Tom Whitlock for the 1986 blockbuster 'Top Gun'.",
        "Won both the Academy Award and Golden Globe for Best Original Song.",
        "Hit #1 in both the UK and the US, becoming Berlin's signature ballad.",
    ],
    ("crowded house", "don't dream it's over"): [
        "Neil Finn wrote the song on his brother Tim's upright piano in Melbourne.",
        "Reached #2 on the US Billboard Hot 100 in 1987, their biggest international hit.",
        "Its surreal video won Best New Artist at the 1987 MTV Video Music Awards.",
    ],
    ("inxs", "need you tonight"): [
        "Andrew Farriss recorded the killer guitar riff in 10 minutes while a cab waited outside.",
        "INXS's only US #1 hit, spending a week atop the Billboard Hot 100 in early 1988.",
        "Its groundbreaking animated video swept five trophies at the 1988 MTV VMAs.",
    ],
    ("the cure", "just like heaven"): [
        "Robert Smith wrote it about a romantic trip to Beachy Head cliffs in southern England.",
        "Became The Cure's first US Top 40 hit, peaking at #40 in 1987.",
        "Smith has repeatedly called it the best pop song The Cure ever recorded.",
    ],
    ("depeche mode", "never let me down again"): [
        "Its thundering drum intro samples Led Zeppelin's 'When the Levee Breaks'.",
        "Live crowds wave their arms in unison during the outro—a tradition since 1987.",
        "Featured prominently in the opening episode of HBO's 'The Last of Us'.",
    ],
    ("new order", "true faith"): [
        "Co-written and produced with Stephen Hague, becoming their first US Top 40 hit.",
        "Its surreal slapping-dancers video was directed by French choreographer Philippe Decouflé.",
        "Peaked at #4 on the UK Singles Chart in July 1987.",
    ],
    ("pixies", "where is my mind?"): [
        "Black Francis wrote it after being chased by a tiny fish while scuba diving in the Caribbean.",
        "Never released as a single in 1988, yet became an alt-rock anthem via 'Fight Club'.",
        "Kim Deal's eerie backing vocals and Joey Santiago's lead line define 80s indie rock.",
    ],
    ("the stone roses", "fools gold"): [
        "Built around a 9-minute funk groove driven by Reni's iconic Clyde Stubblefield-style beat.",
        "Launched the 'Madchester' baggy movement straight into the UK Top 10 in 1989.",
        "Originally released as a double A-side with 'What the World Is Waiting For'.",
    ],
    ("the b-52's", "love shack"): [
        "Inspired by a real tin-roofed cabin near Athens, Georgia where Kate Pierson once lived.",
        "Reached #2 in the UK and #3 in the US, becoming the band's biggest global hit.",
        "A young RuPaul makes a cameo dancing in the colorful 1989 music video.",
    ],
    ("depeche mode", "enjoy the silence"): [
        "Martin Gore originally wrote it as a slow, mournful harmonium ballad.",
        "Alan Wilder suggested speeding it up into an upbeat synth anthem—saving the track.",
        "Won Best British Single at the 1991 Brit Awards and peaked at UK #6.",
    ],
    ("sinead o'connor", "nothing compares 2 u"): [
        "Written by Prince in 1985 for his side project The Family before Sinéad made it iconic.",
        "The single tear in the close-up video was real—Sinéad was thinking of her late mother.",
        "Hit #1 in over 15 countries, spending four weeks atop both the UK and US charts.",
    ],
    ("happy mondays", "step on"): [
        "A baggy Madchester rework of John Kongos's 1971 psychedelic hit 'He's Gonna Step on You Again'.",
        "Produced by Paul Oakenfold and Steve Osborne, peaking at UK #5 in 1990.",
        "Shaun Ryder's 'You're twistin' my melon, man' became a catchphrase of 90s Britain.",
    ],
    ("massive attack", "unfinished sympathy"): [
        "Pioneered the Bristol trip-hop sound with a 40-piece string section from Abbey Road.",
        "Its famous one-take music video follows singer Shara Nelson down West Pico Blvd in LA.",
        "During the 1991 Gulf War, the BBC temporarily credited the band simply as 'Massive'.",
    ],
    ("nirvana", "smells like teen spirit"): [
        "Named after a teen deodorant brand after Kathleen Hanna wrote 'Kurt smells like Teen Spirit' on his wall.",
        "Knocked Michael Jackson's 'Dangerous' off the top of the US album chart in early 1992.",
        "Its pep-rally video was filmed in Culver City with real Nirvana fans recruited at a show.",
    ],
    ("r.e.m.", "losing my religion"): [
        "The title is a Southern US idiom meaning 'at the end of one's rope', not about faith.",
        "Built around Peter Buck learning to play a mandolin while watching TV.",
        "R.E.M.'s highest-charting US single ever, peaking at #4 on the Billboard Hot 100.",
    ],
    ("electronic", "getting away with it"): [
        "Supergroup pairing New Order's Bernard Sumner with The Smiths' Johnny Marr.",
        "Features Neil Tennant of Pet Shop Boys on co-lead vocals and co-writing duties.",
        "Reached #12 in the UK and #38 in the US in late 1989.",
    ],
    ("snap!", "rhythm is a dancer"): [
        "Became the second best-selling UK single of 1992, spending six weeks at #1.",
        "Features the famously cheeky lyric 'I'm as serious as cancer when I say rhythm is a dancer'.",
        "Driven by an iconic Roland Juno-106 synth riff that defined 90s Eurodance.",
    ],
    ("suede", "animal nitrate"): [
        "Sparked the 1993 Britpop explosion and peaked at #7 on the UK Singles Chart.",
        "Its title is a play on the club drug amyl nitrite.",
        "Suede's electrifying 1993 Brit Awards performance made them overnight UK stars.",
    ],
    ("blur", "girls & boys"): [
        "Damon Albarn wrote it while on holiday in Magaluf, Spain observing package-tour nightlife.",
        "Blur's first UK Top 5 single, blending Britpop guitars with Euro-disco synths.",
        "Pet Shop Boys remixed the track and performed it live on tour in 1994.",
    ],
    ("oasis", "live forever"): [
        "Noel Gallagher wrote it as an optimistic antidote to the gloom of early-90s grunge.",
        "Oasis's first UK Top 10 hit, peaking at #10 in August 1994.",
        "Its sleeve features John Lennon's childhood home at 251 Menlove Avenue in Liverpool.",
    ],
    ("portishead", "glory box"): [
        "Built around a slow, smoky sample of Isaac Hayes's 1971 track 'Ike's Rap II'.",
        "Peaked at #13 on the UK Singles Chart and defined 1990s Bristol trip-hop.",
        "Beth Gibbons's haunting vocal was recorded in a single late-night studio take.",
    ],
    ("pulp", "common people"): [
        "Inspired by a wealthy art student Jarvis Cocker met at Central Saint Martins in London.",
        "Peaked at UK #2 in 1995 and became the defining working-class anthem of Britpop.",
        "Voted the #1 Britpop song of all time in a 2014 BBC Radio 6 Music listeners' poll.",
    ],
    ("garbage", "stupid girl"): [
        "Built on a looped drum sample from The Clash's 1979 classic 'Train in Vain'.",
        "Peaked at #4 in the UK and earned two Grammy nominations in 1997.",
        "Co-produced by Butch Vig, the studio mastermind behind Nirvana's 'Nevermind'.",
    ],
    ("underworld", "born slippy .nuxx"): [
        "Rocketed to UK #2 in 1996 after closing Danny Boyle's cult film 'Trainspotting'.",
        "Karl Hyde's fragmented lyrics were scribbled during a wild night out in Soho, London.",
        "Sold over a million copies worldwide, becoming one of the 90s' biggest rave anthems.",
    ],
    ("the prodigy", "firestarter"): [
        "The first Prodigy single to feature Keith Flint on lead vocals—and it hit UK #1.",
        "Samples The Breeders' 'S.O.S.' guitar riff and Art of Noise's 'Close (To the Edit)'.",
        "Its monochrome London Underground video drew record BBC complaints after Top of the Pops.",
    ],
    # --- Wildcards & Cult Classics ---
    ("the normal", "warm leatherette"): [
        "Recorded in a bedroom by Mute Records founder Daniel Miller on a Korg 700S synth.",
        "Inspired by J.G. Ballard's 1973 novel 'Crash' and became a blueprint for industrial pop.",
        "Grace Jones famously covered it in 1980 for her landmark album of the same name.",
    ],
    ("magazine", "shot by both sides"): [
        "Howard Devoto formed Magazine right after leaving Buzzcocks in 1977.",
        "The razor-sharp guitar riff was co-written with Buzzcocks' Pete Shelley.",
        "Widely hailed as one of the very first post-punk singles of 1978.",
    ],
    ("wire", "outdoor miner"): [
        "A 1 minute 44 second post-punk gem about the serpentine leaf miner insect.",
        "Originally had just one verse until EMI asked Wire to extend it for single release.",
        "Peaked at #51 in the UK in 1979 and became a cult favorite among indie bands.",
    ],
    ("tubeway army", "down in the park"): [
        "Features Gary Numan's dystopian sci-fi vision of a robot-run murder park.",
        "Became a cult classic covered by Foo Fighters, Marilyn Manson, and Beck.",
        "One of the earliest tracks to pair heavy Minimoog synths with punk attitude.",
    ],
    ("fad gadget", "back to nature"): [
        "Frank Tovey's 1979 debut single—the very first artist signed to Daniel Miller's Mute Records.",
        "Recorded using primitive analog synths and household objects for percussion.",
        "Helped lay the foundation for Depeche Mode and the entire 80s industrial scene.",
    ],
    ("visage", "fade to grey"): [
        "Spearheaded the London Blitz Club New Romantic movement in late 1980.",
        "Features French spoken-word vocals by Brigitte Arendt alongside Steve Strange.",
        "Co-written by Ultravox's Midge Ure and Billy Currie, hitting UK #8.",
    ],
    ("john foxx", "underpass"): [
        "John Foxx's first solo single after leaving Ultravox, peaking at UK #31 in 1980.",
        "Inspired by J.G. Ballard's concrete urban landscapes and desolate motorways.",
        "Recorded entirely with analog synthesizers and a Roland CR-78 drum machine.",
    ],
    ("a certain ratio", "shack up"): [
        "A gritty Manchester post-punk funk cover of Banbarra's 1975 funk rarity.",
        "Released on Tony Wilson's legendary Factory Records in 1980.",
        "Bridged the gap between Joy Division's gloom and the later Hacienda dance scene.",
    ],
    ("japan", "ghosts"): [
        "An eerily sparse, beatless art-pop single that shocked charts by hitting UK #5 in 1982.",
        "Driven by Richard Barbieri's haunting Prophet-5 synth textures and David Sylvian's baritone.",
        "Widely considered one of the most unconventional UK Top 5 singles of the 1980s.",
    ],
    ("landscape", "einstein a go-go"): [
        "One of the first UK Top 5 hits built entirely around electronic drums and Roland synths.",
        "Its quirky Cold War lyrics warned people to 'throw away the keys' to nuclear silos.",
        "Frontman Richard James Burgess later produced Spandau Ballet's debut hits.",
    ],
    ("bauhaus", "bela lugosi's dead"): [
        "Recorded live in a single studio take in January 1979—and runs over nine minutes long.",
        "Widely credited as the foundational anthem that launched gothic rock.",
        "Featured in the opening scene of the 1983 David Bowie vampire film 'The Hunger'.",
    ],
    ("the associates", "party fears two"): [
        "Billy Mackenzie's operatic vocals carried this Scottish post-punk classic to UK #9.",
        "Its cascading piano hook was played by Alan Rankine in a single inspired session.",
        "Became a defining moment of 1982's adventurous UK pop charts.",
    ],
    ("blancmange", "living on the ceiling"): [
        "Blended 1982 synth-pop with traditional Indian tabla and sitar instrumentation.",
        "Peaked at #7 on the UK Singles Chart and stayed on the chart for 14 weeks.",
        "Its quirky surrealist music video was filmed partly in Egypt.",
    ],
    ("thomas dolby", "she blinded me with science"): [
        "Features real-life British TV scientist Magnus Pyke shouting 'Science!' throughout.",
        "Hit #5 on the US Billboard Hot 100 despite peaking at only #49 in the UK.",
        "Dolby wrote the song specifically as a vehicle to direct his own silent-film style video.",
    ],
    ("yazoo", "situation"): [
        "Originally the B-side to 'Only You' in the UK before US club DJs turned it into a massive hit.",
        "Alison Moyet's iconic laugh on the track has been sampled in dozens of hip-hop and dance songs.",
        "Remixed by François Kevorkian into a #1 US Billboard Dance Club classic.",
    ],
    ("the chameleons", "up the down escalator"): [
        "Standout track from Manchester post-punk cult heroes The Chameleons' 1983 debut.",
        "Known for its twin-guitar atmospheric shimmer that later influenced Interpol and Editors.",
        "John Peel championed the band with multiple legendary BBC Radio 1 sessions.",
    ],
    ("cocteau twins", "pearly-dewdrops' drops"): [
        "Became Cocteau Twins' highest-charting UK single of the 1980s, reaching #29 in 1984.",
        "Elizabeth Fraser's ethereal vocals weave abstract, dreamlike syllables into pure melody.",
        "A cornerstone of 4AD Records' dreamy shoegaze and dream-pop blueprint.",
    ],
    ("the psychedelic furs", "the ghost in you"): [
        "Produced by Keith Forsey, who also produced Simple Minds' 'Don't You (Forget About Me)'.",
        "Became a US alt-radio staple and appeared on the 'Pretty in Pink' era soundtrack circuit.",
        "Richard Butler's raspy vocal made it one of the band's most beloved live anthems.",
    ],
    ("echo & the bunnymen", "the killing moon"): [
        "Ian McCulloch woke up with the chorus fully formed in his head and called it 'the greatest song ever written'.",
        "Peaked at UK #9 in 1984 and gained a new cult following in the 2001 film 'Donnie Darko'.",
        "Will Sergeant's haunting reverse-reverb guitar line anchors the track's dark mood.",
    ],
    ("siouxsie and the banshees", "cities in dust"): [
        "Written about the ancient Roman city of Pompeii buried by Mount Vesuvius in 79 AD.",
        "Peaked at #21 in the UK in 1985 and became a goth-club dancefloor staple.",
        "Its driving synth-percussion groove marked a slicker, pop-leaning era for the Banshees.",
    ],
    ("propaganda", "duel"): [
        "German synth-pop quartet signed to Trevor Horn's ZTT Records alongside Frankie Goes to Hollywood.",
        "Peaked at #21 in the UK in 1985 and was later used as the theme for BBC's 'Top Gear' rallies.",
        "Its companion B-side 'Jewel' is a darker, harder industrial rework of the exact same song.",
    ],
    ("the sisters of mercy", "this corrosion"): [
        "Produced by Meat Loaf collaborator Jim Steinman, complete with a 40-voice New York choir.",
        "Andrew Eldritch wrote the grandiose lyrics partly as a parody of rival goth bands.",
        "Hit #7 on the UK Singles Chart in 1987, the band's biggest commercial breakthrough.",
    ],
    ("the jesus and mary chain", "april skies"): [
        "Became the Scottish noise-pop pioneers' highest-charting UK single, peaking at #8 in 1987.",
        "Traded their trademark feedback wall for a crisp, moody 60s-inspired guitar groove.",
        "Its moody video captures the Reid brothers' deadpan cool at the height of indie's 80s rise.",
    ],
    ("my bloody valentine", "soon"): [
        "Pioneered the 'shoegaze' sound by fusing swirling reverse-reverb guitars with baggy dance beats.",
        "Brian Eno famously praised it as 'the vaguest music ever to have been a hit'.",
        "Closed out their 1991 masterpiece album 'Loveless' after peaking at UK #41 in 1990.",
    ],
    ("ride", "vapour trail"): [
        "Written by Andy Bell, who later joined Oasis as their bassist in 1999.",
        "Ends with an unforgettable lone cello outro fading over shimmering shoegaze guitars.",
        "Named one of the defining Creation Records classics of the early 1990s.",
    ],
    ("slowdive", "alison"): [
        "Lead single from Slowdive's 1993 shoegaze landmark album 'Souvlaki'.",
        "Initially dismissed by 90s UK music press, it is now revered as a genre masterpiece.",
        "Neil Halstead wrote it as a bittersweet snapshot of hazy teenage summers.",
    ],
    ("saint etienne", "only love can break your heart"): [
        "Transformed Neil Young's 1970 folk ballad into a hypnotic 1990 indie-dance classic.",
        "Recorded with guest vocalist Moira Lambert before Sarah Cracknell joined the group.",
        "Peaked at #39 in the UK and launched one of Britain's coolest indie-pop collectives.",
    ],
    ("808 state", "pacific state"): [
        "Named after the Roland TR-808 drum machine and built around a soaring soprano sax hook.",
        "Born out of Manchester's Hacienda acid house scene, hitting UK #10 in 1989.",
        "Originally an ambient chill-out track before DJs turned it into a sunrise rave anthem.",
    ],
    ("the klf", "3 a.m. eternal"): [
        "Hit UK #1 in 1991 and sold millions worldwide with its 'stadium house' rave sound.",
        "Bill Drummond and Jimmy Cauty famously sampled live crowd cheers to fake a stadium gig.",
        "A year later, The KLF deleted their entire back catalog and quit the music industry.",
    ],
    ("orbital", "chime"): [
        "Recorded by brothers Phil and Paul Hartnoll on their dad's cassette deck for under £1.",
        "Peaked at UK #17 in 1990 and brought live improvised techno to Top of the Pops.",
        "Its hypnotic Roland TB-303 melody became one of British rave culture's founding anthems.",
    ],
    ("aphex twin", "didgeridoo"): [
        "Richard D. James originally wrote it to clear the dancefloor at the end of Cornwall raves.",
        "Reached #55 on the UK Singles Chart in 1992 despite being ferocious 140+ BPM acid techno.",
        "Launched Aphex Twin as the most inventive electronic producer of the 1990s.",
    ],
    ("lush", "ladykillers"): [
        "Peaked at #22 in the UK in 1996, marking Lush's pivot from shoegaze to punchy Britpop.",
        "Miki Berenyi wrote the sharp lyrics about pretentious guys hitting on women in pubs.",
        "Its playful video parodied classic 1950s heist films and became an MTV Europe favorite.",
    ],
    ("sleeper", "inbetweener"): [
        "Sleeper's breakthrough UK Top 20 hit, reaching #16 during the height of 1995 Britpop.",
        "Louise Wener wrote it about ordinary suburban life and feeling stuck in the middle.",
        "Its supermarket-set video became a staple of mid-90s UK music television.",
    ],
    ("shed seven", "going for gold"): [
        "York indie band Shed Seven's highest-charting single, peaking at UK #8 in 1996.",
        "Featured on their platinum-selling album 'A Maximum High' during peak Britpop.",
        "Remains a massive singalong finale at UK indie festivals nearly 30 years later.",
    ],
}


def _clean_sentence(text: str, max_len: int = 115) -> str:
    """Normalize whitespace, strip citations/parentheticals, and keep concise."""
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        cut = text[: max_len - 1].rsplit(" ", 1)[0].rstrip(",;:-")
        text = cut + "…"
    return text


def fetch_wikipedia_facts(artist: str, title: str, timeout: int = 8) -> List[str]:
    """
    Query the public English Wikipedia API for the song's article extract
    and pull up to 3 concise, interesting sentences.
    """
    try:
        search_q = f'"{title}" "{artist}" song'
        search_resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": search_q,
                "srlimit": 3,
                "format": "json",
            },
            headers=HEADERS,
            timeout=timeout,
        )
        search_resp.raise_for_status()
        hits = search_resp.json().get("query", {}).get("search", [])
        if not hits:
            return []

        page_id = hits[0]["pageid"]
        extract_resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "pageids": str(page_id),
                "prop": "extracts",
                "exintro": True,
                "explaintext": True,
                "format": "json",
            },
            headers=HEADERS,
            timeout=timeout,
        )
        extract_resp.raise_for_status()
        pages = extract_resp.json().get("query", {}).get("pages", {})
        extract = pages.get(str(page_id), {}).get("extract", "")
        if not extract:
            return []

        # Split into complete sentences (abbreviation-aware) and prefer non-truncated ones
        from core.hooks import split_sentences

        raw_sentences = split_sentences(extract.strip())
        complete_facts: List[str] = []
        truncated_facts: List[str] = []
        for s in raw_sentences:
            cleaned = _clean_sentence(s)
            if len(cleaned) < 28:
                continue
            if cleaned.endswith("…") or cleaned.endswith("..."):
                truncated_facts.append(cleaned)
            else:
                complete_facts.append(cleaned)
            if len(complete_facts) >= 3:
                break
        return (complete_facts + truncated_facts)[:3]
    except Exception as e:
        logger.warning(f"Wikipedia fact lookup failed for {artist} - {title}: {e}")
        return []


def build_metadata_fallback_facts(track: Dict[str, Any]) -> List[str]:
    """Generate 3 clean fallback facts from track metadata."""
    artist = track.get("artist", "Unknown Artist")
    title = track.get("title", "Unknown Track")
    year = track.get("year", "")
    peak = track.get("chart_peak")
    region = track.get("chart_region", "UK")
    genre = track.get("genre", "GenX Classic")
    is_wildcard = bool(track.get("is_wildcard", 0))

    facts = []
    if peak:
        facts.append(f"'{title}' peaked at #{peak} on the {region} singles chart in {year}.")
    else:
        facts.append(f"Released by {artist} in {year} during the golden GenX era.")

    facts.append(f"A signature {genre} cut from {artist}'s {year} catalog.")

    if is_wildcard:
        facts.append(f"Selected as a GenX71 Deep Cut wildcard — a cult gem from {year}.")
    else:
        facts.append(f"An enduring {year} chart favorite still spinning on GenX71 AutoVJ.")

    return facts


def get_or_fetch_track_facts(
    track: Dict[str, Any],
    db_path: Optional[str] = None,
    allow_network: bool = True,
) -> List[str]:
    """
    Return exactly 3 concise facts for a track:
      1. Cached facts in track['facts_json']
      2. Curated GenX catalog facts
      3. Wikipedia API lookup (if allow_network=True)
      4. Metadata fallback for any remaining slots
    Saves resolved facts back to SQLite if track['id'] is present.
    """
    # 1. Cached in DB
    raw_cached = track.get("facts_json")
    if raw_cached:
        try:
            parsed = json.loads(raw_cached)
            if isinstance(parsed, list) and len(parsed) >= 3 and all(isinstance(x, str) and x.strip() for x in parsed[:3]):
                return [x.strip() for x in parsed[:3]]
        except (ValueError, TypeError):
            pass

    artist = str(track.get("artist", "")).strip()
    title = str(track.get("title", "")).strip()
    key = (artist.lower(), title.lower())

    facts: List[str] = []

    # 2. Curated catalog
    if key in CURATED_TRACK_FACTS:
        facts = list(CURATED_TRACK_FACTS[key][:3])
    else:
        # 3. Wikipedia lookup for custom tracks
        if allow_network and artist and title:
            facts = fetch_wikipedia_facts(artist, title)

    # 4. Pad with metadata fallbacks if fewer than 3
    if len(facts) < 3:
        fallbacks = build_metadata_fallback_facts(track)
        for fb in fallbacks:
            if len(facts) >= 3:
                break
            if fb not in facts:
                facts.append(fb)

    facts = facts[:3]

    # Persist to SQLite so future lookups are instant
    track_id = track.get("id")
    if track_id is not None:
        try:
            update_track_facts(int(track_id), json.dumps(facts), db_path=db_path)
        except Exception as e:
            logger.warning(f"Could not cache facts for track {track_id}: {e}")

    return facts
