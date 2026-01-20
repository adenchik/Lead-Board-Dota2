FROM python:3.11.0

WORKDIR /leadboarddota2

COPY ./requirements.txt ./

RUN pip install --no-cache-dir --upgrade -r requirements.txt

COPY . .

CMD ["fastapi", "run", "./main.py"]