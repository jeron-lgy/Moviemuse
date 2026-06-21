New-Item -ItemType Directory -Force -Path "sample-media\Avatar" | Out-Null
Set-Content -LiteralPath "sample-media\Avatar\Avatar.2009.1080p-c.mkv" -Value ""
Set-Content -LiteralPath "sample-media\Avatar\Avatar.2009.1080p-c.nfo" -Value "<movie><title>Avatar</title><year>2009</year><uniqueid type=""tmdb"">19995</uniqueid></movie>"
Set-Content -LiteralPath "sample-media\Avatar\Avatar.2009.4K.mkv" -Value ""
Set-Content -LiteralPath "sample-media\Avatar\Avatar.2009.4K.nfo" -Value "<movie><title>Avatar</title><year>2009</year><uniqueid type=""tmdb"">19995</uniqueid></movie>"
Set-Content -LiteralPath "sample-media\Avatar\Avatar.2009.4K-c.srt" -Value "1"

New-Item -ItemType Directory -Force -Path "sample-media\Mixed" | Out-Null
Set-Content -LiteralPath "sample-media\Mixed\Inception.2010.1080p.mp4" -Value ""
Set-Content -LiteralPath "sample-media\Mixed\Inception.2010.1080p.nfo" -Value "<movie><title>Inception</title><year>2010</year><uniqueid type=""imdb"">tt1375666</uniqueid></movie>"
Set-Content -LiteralPath "sample-media\Mixed\Inception.2010.4K-c.mp4" -Value ""
Set-Content -LiteralPath "sample-media\Mixed\Inception.2010.4K-c.nfo" -Value "<movie><title>Inception</title><year>2010</year><uniqueid type=""imdb"">tt1375666</uniqueid></movie>"

Write-Host "Sample media created: sample-media"
