from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from controle_acesso.permissions import IsSuperintendente
from django.conf import settings
from django.db.models import Sum, Count
from datetime import date, timedelta
from django.db.models.functions import TruncMonth, TruncWeek
import json
import logging

from openai import OpenAI
from operacao_voo.models import Embarque, Desembarque
from controle_acesso.models import EventoCatraca
from sala_briefing.models import BriefingSession
from transporte.models import RegistroTransporte

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsSuperintendente])
def central_analise_status(request):
    """Status do módulo Central de Análise"""
    return Response({
        'status': 'ok',
        'module': 'central_analise',
        'message': 'Módulo Central de Análise ativo',
    })


# =========================================================
# SYSTEM PROMPT
# =========================================================
SYSTEM_PROMPT = """Você é o assistente de análise de dados do sistema OnePax, \
um sistema de gestão de operações de heliporto offshore.

Você tem acesso a dados de:
- Embarques (voos de saída com contagem de passageiros)
- Desembarques (voos de chegada com contagem de passageiros)
- Eventos de Catraca (giros registrados nas catracas de embarque/desembarque)
- Sessões de Briefing
- Registros de Transporte

Quando o usuário perguntar sobre dados, use as funções disponíveis para consultar o banco de dados.
Sempre responda em português brasileiro. Seja preciso com números e datas.
Quando nenhuma data for especificada e o usuário perguntar "hoje", use a data atual.
A data atual é: {today}.
"""

# =========================================================
# OPENAI TOOLS (FUNCTION CALLING)
# =========================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "contar_embarques",
            "description": "Conta registros de embarque e soma total de passageiros. Pode filtrar por data, faixa de horário, operadora, número do voo ou plataforma.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "Data específica no formato YYYY-MM-DD"},
                    "data_inicio": {"type": "string", "description": "Data inicial (YYYY-MM-DD) para filtro por período"},
                    "data_fim": {"type": "string", "description": "Data final (YYYY-MM-DD) para filtro por período"},
                    "hora_inicio": {"type": "string", "description": "Hora inicial (HH:MM) para filtro por horário"},
                    "hora_fim": {"type": "string", "description": "Hora final (HH:MM) para filtro por horário"},
                    "operadora": {"type": "string", "description": "Nome da operadora aérea"},
                    "flight_number": {"type": "string", "description": "Número do voo"},
                    "platform": {"type": "string", "description": "Nome da plataforma"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contar_desembarques",
            "description": "Conta registros de desembarque e soma total de passageiros. Pode filtrar por data, faixa de horário, operadora ou número do voo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "Data específica (YYYY-MM-DD)"},
                    "data_inicio": {"type": "string", "description": "Data inicial (YYYY-MM-DD)"},
                    "data_fim": {"type": "string", "description": "Data final (YYYY-MM-DD)"},
                    "hora_inicio": {"type": "string", "description": "Hora inicial (HH:MM)"},
                    "hora_fim": {"type": "string", "description": "Hora final (HH:MM)"},
                    "operadora": {"type": "string", "description": "Nome da operadora aérea"},
                    "flight_number": {"type": "string", "description": "Número do voo"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_embarques",
            "description": "Lista registros de embarque com detalhes: número do voo, aeronave, operadora, data, hora, plataforma, passageiros, cliente final. Limitado a 50 resultados.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "Data específica (YYYY-MM-DD)"},
                    "data_inicio": {"type": "string", "description": "Data inicial (YYYY-MM-DD)"},
                    "data_fim": {"type": "string", "description": "Data final (YYYY-MM-DD)"},
                    "operadora": {"type": "string", "description": "Operadora aérea"},
                    "flight_number": {"type": "string", "description": "Número do voo"},
                    "limite": {"type": "integer", "description": "Máximo de resultados (padrão 20, máximo 50)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_desembarques",
            "description": "Lista registros de desembarque com detalhes: número do voo, aeronave, operadora, data, hora, origem, passageiros, cliente final. Limitado a 50 resultados.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "Data específica (YYYY-MM-DD)"},
                    "data_inicio": {"type": "string", "description": "Data inicial (YYYY-MM-DD)"},
                    "data_fim": {"type": "string", "description": "Data final (YYYY-MM-DD)"},
                    "operadora": {"type": "string", "description": "Operadora aérea"},
                    "flight_number": {"type": "string", "description": "Número do voo"},
                    "limite": {"type": "integer", "description": "Máximo de resultados (padrão 20, máximo 50)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contar_eventos_catraca",
            "description": "Conta eventos de giro nas catracas. Pode filtrar por período (data/hora), tipo de catraca (EMBARQUE ou DESEMBARQUE) ou identificador específico da catraca.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data_inicio": {"type": "string", "description": "Início do período (YYYY-MM-DD ou YYYY-MM-DD HH:MM)"},
                    "data_fim": {"type": "string", "description": "Fim do período (YYYY-MM-DD ou YYYY-MM-DD HH:MM)"},
                    "tipo_catraca": {"type": "string", "enum": ["EMBARQUE", "DESEMBARQUE"], "description": "Tipo da catraca"},
                    "identificador_catraca": {"type": "string", "description": "Identificador da catraca (ex: 1001)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_briefings",
            "description": "Lista sessões de briefing. Retorna companhia aérea, cliente final, data, número do voo, horário, serviço, solicitante.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "Data específica (YYYY-MM-DD)"},
                    "data_inicio": {"type": "string", "description": "Data inicial (YYYY-MM-DD)"},
                    "data_fim": {"type": "string", "description": "Data final (YYYY-MM-DD)"},
                    "companhia_aerea": {"type": "string", "description": "Companhia aérea"},
                    "limite": {"type": "integer", "description": "Máximo de resultados (padrão 20, máximo 50)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resumo_operacional",
            "description": "Retorna um resumo operacional completo: total de embarques, desembarques, eventos de catraca, briefings e transportes para uma data ou período.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "Data específica (YYYY-MM-DD). Padrão: hoje."},
                    "data_inicio": {"type": "string", "description": "Data inicial para período (YYYY-MM-DD)"},
                    "data_fim": {"type": "string", "description": "Data final para período (YYYY-MM-DD)"},
                },
                "required": [],
            },
        },
    },
]


# =========================================================
# ORM QUERY FUNCTIONS
# =========================================================
def _apply_embarque_filters(qs, args):
    if args.get("data"):
        qs = qs.filter(departure_date=args["data"])
    if args.get("data_inicio"):
        qs = qs.filter(departure_date__gte=args["data_inicio"])
    if args.get("data_fim"):
        qs = qs.filter(departure_date__lte=args["data_fim"])
    if args.get("hora_inicio"):
        qs = qs.filter(departure_time__gte=args["hora_inicio"])
    if args.get("hora_fim"):
        qs = qs.filter(departure_time__lte=args["hora_fim"])
    if args.get("operadora"):
        qs = qs.filter(operadora__icontains=args["operadora"])
    if args.get("flight_number"):
        qs = qs.filter(flight_number__icontains=args["flight_number"])
    if args.get("platform"):
        qs = qs.filter(platform__icontains=args["platform"])
    return qs


def _apply_desembarque_filters(qs, args):
    if args.get("data"):
        qs = qs.filter(arrival_date=args["data"])
    if args.get("data_inicio"):
        qs = qs.filter(arrival_date__gte=args["data_inicio"])
    if args.get("data_fim"):
        qs = qs.filter(arrival_date__lte=args["data_fim"])
    if args.get("hora_inicio"):
        qs = qs.filter(arrival_time__gte=args["hora_inicio"])
    if args.get("hora_fim"):
        qs = qs.filter(arrival_time__lte=args["hora_fim"])
    if args.get("operadora"):
        qs = qs.filter(operadora__icontains=args["operadora"])
    if args.get("flight_number"):
        qs = qs.filter(flight_number__icontains=args["flight_number"])
    return qs


def _contar_embarques(args):
    qs = _apply_embarque_filters(Embarque.objects.all(), args)
    total_voos = qs.count()
    total_pax = qs.aggregate(total=Sum("passengers_boarded"))["total"] or 0
    return json.dumps({"total_voos": total_voos, "total_passageiros_embarcados": total_pax})


def _contar_desembarques(args):
    qs = _apply_desembarque_filters(Desembarque.objects.all(), args)
    total_voos = qs.count()
    total_pax = qs.aggregate(total=Sum("passengers_disembarked"))["total"] or 0
    return json.dumps({"total_voos": total_voos, "total_passageiros_desembarcados": total_pax})


def _listar_embarques(args):
    qs = _apply_embarque_filters(Embarque.objects.all(), args)
    limite = min(args.get("limite", 20), 50)
    records = list(qs.order_by("-departure_date", "-departure_time")[:limite].values(
        "flight_number", "aeronave", "operadora", "departure_date",
        "departure_time", "platform", "passengers_boarded", "cliente_final",
    ))
    for r in records:
        r["departure_date"] = str(r["departure_date"])
        r["departure_time"] = str(r["departure_time"])
    return json.dumps({"registros": records, "total": len(records)})


def _listar_desembarques(args):
    qs = _apply_desembarque_filters(Desembarque.objects.all(), args)
    limite = min(args.get("limite", 20), 50)
    records = list(qs.order_by("-arrival_date", "-arrival_time")[:limite].values(
        "flight_number", "aeronave", "operadora", "arrival_date",
        "arrival_time", "origin", "passengers_disembarked", "cliente_final",
    ))
    for r in records:
        r["arrival_date"] = str(r["arrival_date"])
        r["arrival_time"] = str(r["arrival_time"])
    return json.dumps({"registros": records, "total": len(records)})


def _contar_eventos_catraca(args):
    qs = EventoCatraca.objects.all()
    if args.get("data_inicio"):
        qs = qs.filter(timestamp__gte=args["data_inicio"])
    if args.get("data_fim"):
        qs = qs.filter(timestamp__lte=args["data_fim"])
    if args.get("tipo_catraca"):
        qs = qs.filter(catraca__tipo=args["tipo_catraca"])
    if args.get("identificador_catraca"):
        qs = qs.filter(catraca__identificador=args["identificador_catraca"])
    total = qs.count()
    por_catraca = list(qs.values("catraca__nome", "catraca__tipo").annotate(total=Count("id")))
    return json.dumps({"total_eventos": total, "por_catraca": por_catraca})


def _listar_briefings(args):
    qs = BriefingSession.objects.all()
    if args.get("data"):
        qs = qs.filter(data=args["data"])
    if args.get("data_inicio"):
        qs = qs.filter(data__gte=args["data_inicio"])
    if args.get("data_fim"):
        qs = qs.filter(data__lte=args["data_fim"])
    if args.get("companhia_aerea"):
        qs = qs.filter(companhia_aerea__icontains=args["companhia_aerea"])
    limite = min(args.get("limite", 20), 50)
    records = list(qs.order_by("-data", "-horario")[:limite].values(
        "companhia_aerea", "cliente_final", "data", "numero_voo",
        "horario", "servico", "solicitante",
    ))
    for r in records:
        r["data"] = str(r["data"]) if r["data"] else None
        r["horario"] = str(r["horario"]) if r["horario"] else None
    return json.dumps({"registros": records, "total": len(records)})


def _resumo_operacional(args):
    target_date = args.get("data", str(date.today()))
    data_inicio = args.get("data_inicio", target_date)
    data_fim = args.get("data_fim", target_date)

    embarques = Embarque.objects.filter(departure_date__gte=data_inicio, departure_date__lte=data_fim)
    desembarques = Desembarque.objects.filter(arrival_date__gte=data_inicio, arrival_date__lte=data_fim)

    return json.dumps({
        "periodo": {"inicio": data_inicio, "fim": data_fim},
        "embarques": {
            "total_voos": embarques.count(),
            "total_passageiros": embarques.aggregate(t=Sum("passengers_boarded"))["t"] or 0,
        },
        "desembarques": {
            "total_voos": desembarques.count(),
            "total_passageiros": desembarques.aggregate(t=Sum("passengers_disembarked"))["t"] or 0,
        },
        "eventos_catraca": EventoCatraca.objects.filter(
            timestamp__date__gte=data_inicio, timestamp__date__lte=data_fim
        ).count(),
        "briefings": BriefingSession.objects.filter(
            data__gte=data_inicio, data__lte=data_fim
        ).count(),
        "transportes": RegistroTransporte.objects.filter(
            data__gte=data_inicio, data__lte=data_fim
        ).count(),
    })


TOOL_DISPATCH = {
    "contar_embarques": _contar_embarques,
    "contar_desembarques": _contar_desembarques,
    "listar_embarques": _listar_embarques,
    "listar_desembarques": _listar_desembarques,
    "contar_eventos_catraca": _contar_eventos_catraca,
    "listar_briefings": _listar_briefings,
    "resumo_operacional": _resumo_operacional,
}


def execute_tool(tool_name, args):
    try:
        fn = TOOL_DISPATCH.get(tool_name)
        if not fn:
            return json.dumps({"error": f"Função desconhecida: {tool_name}"})
        return fn(args)
    except Exception as e:
        logger.error(f"Erro ao executar tool {tool_name}: {e}")
        return json.dumps({"error": str(e)})


# =========================================================
# CHAT VIEW
# =========================================================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def chat_view(request):
    user_message = request.data.get('message', '').strip()
    history = request.data.get('history', [])

    if not user_message:
        return Response({'error': 'Mensagem vazia'}, status=400)

    if not settings.OPENAI_API_KEY:
        return Response({'error': 'Chave da OpenAI não configurada'}, status=500)

    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(today=str(date.today()))}
    ]

    for msg in history[-20:]:
        messages.append({
            "role": msg.get("role", "user"),
            "content": msg.get("content", ""),
        })

    messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.3,
            max_tokens=2000,
        )

        assistant_message = response.choices[0].message

        iterations = 0
        while assistant_message.tool_calls and iterations < 5:
            iterations += 1
            messages.append(assistant_message.model_dump())

            for tool_call in assistant_message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                logger.info(f"Tool call: {fn_name}({fn_args})")
                result = execute_tool(fn_name, fn_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=2000,
            )
            assistant_message = response.choices[0].message

        reply = assistant_message.content or "Desculpe, não consegui gerar uma resposta."
        return Response({'reply': reply})

    except Exception as e:
        logger.error(f"Erro no chat: {e}")
        return Response({'error': 'Erro ao processar mensagem. Tente novamente.'}, status=500)


# =========================================================
# DASHBOARD API
# =========================================================
def _calc_change(current, previous):
    if not previous:
        return None
    return round(((current - previous) / previous) * 100, 1)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_data(request):
    today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)
    two_weeks_ago = today - timedelta(days=14)
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    # --- KPIs ---
    pax_emb_hoje = Embarque.objects.filter(departure_date=today).aggregate(t=Sum('passengers_boarded'))['t'] or 0
    pax_desemb_hoje = Desembarque.objects.filter(arrival_date=today).aggregate(t=Sum('passengers_disembarked'))['t'] or 0
    pax_hoje = pax_emb_hoje + pax_desemb_hoje

    pax_emb_ontem = Embarque.objects.filter(departure_date=yesterday).aggregate(t=Sum('passengers_boarded'))['t'] or 0
    pax_desemb_ontem = Desembarque.objects.filter(arrival_date=yesterday).aggregate(t=Sum('passengers_disembarked'))['t'] or 0
    pax_ontem = pax_emb_ontem + pax_desemb_ontem

    voos_hoje = Embarque.objects.filter(departure_date=today).count() + Desembarque.objects.filter(arrival_date=today).count()
    voos_semana = Embarque.objects.filter(departure_date__gte=week_ago).count() + Desembarque.objects.filter(arrival_date__gte=week_ago).count()
    voos_semana_ant = Embarque.objects.filter(departure_date__gte=two_weeks_ago, departure_date__lt=week_ago).count() + \
                      Desembarque.objects.filter(arrival_date__gte=two_weeks_ago, arrival_date__lt=week_ago).count()

    # --- Movimentação por operadora (cada período) ---
    def pax_by_operator(date_start, date_end):
        return list(Embarque.objects.filter(
            departure_date__gte=date_start, departure_date__lte=date_end
        ).values('operadora').annotate(
            passageiros=Sum('passengers_boarded'), voos=Count('id')
        ).order_by('-passageiros'))

    movimentacao = {
        'diario': pax_by_operator(today, today),
        'semanal': pax_by_operator(week_ago, today),
        'mensal': pax_by_operator(month_start, today),
        'anual': pax_by_operator(year_start, today),
    }

    # --- Distribuição por horário (hoje) ---
    por_horario = []
    for h in [6, 8, 10, 12, 14, 16, 18]:
        h_next = h + 2
        emb = Embarque.objects.filter(
            departure_date=today, departure_time__gte=f'{h:02d}:00', departure_time__lt=f'{h_next:02d}:00'
        ).aggregate(t=Sum('passengers_boarded'))['t'] or 0
        desemb = Desembarque.objects.filter(
            arrival_date=today, arrival_time__gte=f'{h:02d}:00', arrival_time__lt=f'{h_next:02d}:00'
        ).aggregate(t=Sum('passengers_disembarked'))['t'] or 0
        por_horario.append({'horario': f'{h:02d}:00', 'embarques': emb, 'desembarques': desemb})

    # --- Uso de aeronaves (últimos 30 dias) ---
    aeronaves = list(Embarque.objects.filter(
        departure_date__gte=today - timedelta(days=30)
    ).values('aeronave').annotate(total=Count('id')).order_by('-total')[:10])

    # --- Ranking de operadores (hoje) ---
    operadores = list(Embarque.objects.filter(
        departure_date=today
    ).values('operadora').annotate(
        passageiros=Sum('passengers_boarded'), voos=Count('id')
    ).order_by('-passageiros'))

    return Response({
        'kpis': {
            'passageiros_hoje': pax_hoje,
            'voos_hoje': voos_hoje,
            'variacao_pax': _calc_change(pax_hoje, pax_ontem),
            'variacao_voos': _calc_change(voos_semana, voos_semana_ant),
        },
        'movimentacao': movimentacao,
        'por_horario': por_horario,
        'aeronaves': aeronaves,
        'operadores': operadores,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def operador_detail(request, operadora):
    today = date.today()

    all_emb = Embarque.objects.filter(operadora__icontains=operadora)
    total_pax = all_emb.aggregate(t=Sum('passengers_boarded'))['t'] or 0
    total_voos = all_emb.count()
    first = all_emb.order_by('departure_date').first()
    dias_operando = (today - first.departure_date).days + 1 if first else 0
    pax_por_voo = round(total_pax / total_voos) if total_voos else 0

    # Mensal (últimos 12 meses)
    mensal = list(all_emb.filter(
        departure_date__gte=today - timedelta(days=365)
    ).annotate(month=TruncMonth('departure_date')).values('month').annotate(
        passageiros=Sum('passengers_boarded'), voos=Count('id')
    ).order_by('month'))
    for item in mensal:
        item['name'] = item.pop('month').strftime('%b')

    # Semanal (últimas 12 semanas)
    semanal = list(all_emb.filter(
        departure_date__gte=today - timedelta(weeks=12)
    ).annotate(week=TruncWeek('departure_date')).values('week').annotate(
        passageiros=Sum('passengers_boarded'), voos=Count('id')
    ).order_by('week'))
    for i, item in enumerate(semanal):
        item['name'] = f'S{i + 1}'
        del item['week']

    # Anual (últimos 5 anos)
    anual = list(all_emb.filter(
        departure_date__year__gte=today.year - 4
    ).values('departure_date__year').annotate(
        passageiros=Sum('passengers_boarded'), voos=Count('id')
    ).order_by('departure_date__year'))
    for item in anual:
        item['name'] = str(item.pop('departure_date__year'))

    # Top rotas (plataformas)
    rotas = list(all_emb.values('platform').annotate(
        passageiros=Sum('passengers_boarded')
    ).order_by('-passageiros')[:4])

    return Response({
        'kpis': {
            'dias_operando': dias_operando,
            'total_passageiros': total_pax,
            'total_voos': total_voos,
            'pax_por_voo': pax_por_voo,
        },
        'mensal': mensal,
        'semanal': semanal,
        'anual': anual,
        'rotas': rotas,
    })
