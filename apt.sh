apt update && sudo apt install npm -y && npm install pm2 -g && pip install ccxt pandas pandas_ta && pip install ccxt pandas numpy colorama && pip install ccxt pandas pandas_ta mplfinance requests
cd bbmav1 
cd bbmav1
sudo pm2 start bbmav2-15m.py --name "bot-bbma-pyv2-15m" --interpreter python3 && sudo pm2 start bbmav2-4h.py --name "bot-bbma-py4h" --interpreter python3 && sudo pm2 start bbma1h.py --name "bot-bbma-pyv21h" --interpreter python3 && sudo pm2 start bbmav1.py --name "bot-bbma-pyv1" --interpreter python3 
cd bbmav2
sudo pm2 start bbmav2f.py --name "bot-bbma-pyv2" --interpreter python3 && pm2 logs bot-bbma-pyv2-15m 
